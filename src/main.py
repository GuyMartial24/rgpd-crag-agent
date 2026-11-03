"""API REST (FastAPI) qui expose l'agent CRAG RGPD.

Lancement en local :
    uvicorn src.main:app --reload

Documentation interactive : http://localhost:8000/docs
"""

import json
import logging
import time
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from ollama import ResponseError
from pydantic import BaseModel, Field

from src import __version__
from src.config import get_settings
from src.graph import CragAgent, create_default_agent

# Affiche les messages d'information (durée de chaque étape de l'agent)
# dans les logs du conteneur : docker compose logs api
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Message renvoyé quand le modèle met trop de temps à répondre.
TIMEOUT_MESSAGE = "Le modèle de langage a mis trop de temps à répondre."

app = FastAPI(
    title="RGPD CRAG Agent",
    description=(
        "Questions-réponses sur le RGPD via un agent Corrective RAG "
        "(LangGraph + Ollama)."
    ),
    version=__version__,
)

# CORS : autorise les navigateurs à appeler l'API depuis un autre site
# (par exemple quand le widget de chat est intégré sur un site client).
# Sans cela, le navigateur bloque les requêtes venant d'un autre domaine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ---------------------------------------------------------------------
# Modèles de données (Pydantic)
# Ils décrivent le format des requêtes et des réponses. FastAPI les
# utilise pour valider les données reçues et générer la documentation.
# ---------------------------------------------------------------------


class Exchange(BaseModel):
    """Un échange passé de la conversation : une question et sa réponse."""

    question: str = Field(max_length=1000)
    answer: str = Field(max_length=5000)


class AskRequest(BaseModel):
    """Corps de la requête POST /ask."""

    # La question doit contenir entre 3 et 1000 caractères.
    question: str = Field(
        min_length=3,
        max_length=1000,
        examples=["Et pour les mineurs ?"],
    )
    # Seules les valeurs « fr » et « en » sont acceptées.
    language: Literal["fr", "en"] = Field(
        default="fr",
        description="Langue du texte du RGPD interrogé et de la réponse",
    )
    # Mémoire de conversation : le client renvoie les échanges précédents
    # (du plus ancien au plus récent). 20 échanges maximum par requête ;
    # l'agent n'utilise que les plus récents (paramètre MAX_HISTORY).
    history: list[Exchange] = Field(
        default_factory=list,
        max_length=20,
        examples=[
            [
                {
                    "question": "Quand le consentement est-il valable ?",
                    "answer": "Le consentement doit être libre, "
                    "spécifique, éclairé et univoque (Article 4)...",
                }
            ]
        ],
    )


class AskResponse(BaseModel):
    """Réponse renvoyée par POST /ask."""

    answer: str
    sources: list[str]
    # Question réécrite à partir de la conversation (identique à la
    # question posée s'il n'y a pas d'historique).
    standalone_question: str
    search_query: str
    rewrites: int


# ---------------------------------------------------------------------
# Création de l'agent
# ---------------------------------------------------------------------

# L'agent est créé une seule fois, au premier appel, puis réutilisé.
_agent = None


def get_agent() -> CragAgent:
    """Retourne l'agent (fonction utilisée comme dépendance FastAPI).

    Dans les tests, cette fonction est remplacée par un faux agent grâce
    à app.dependency_overrides.
    """
    global _agent
    if _agent is None:
        _agent = create_default_agent()
    return _agent


# ---------------------------------------------------------------------
# Routes de l'API
# ---------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Vérifie que l'API répond (utilisé par Docker)."""
    return {"status": "ok", "version": __version__}


@app.post("/ask", response_model=AskResponse)
def ask_question(
    request: AskRequest,
    # Depends(get_agent) : FastAPI appelle get_agent() et nous donne
    # l'agent en paramètre.
    agent: Annotated[CragAgent, Depends(get_agent)],
) -> AskResponse:
    """Pose une question sur le RGPD à l'agent (réponse complète)."""
    history = history_to_dicts(request)

    start = time.perf_counter()
    try:
        result = agent.ask(request.question, request.language, history)
    except httpx.TimeoutException as error:
        # Le modèle a mis plus de REQUEST_TIMEOUT secondes à répondre :
        # code 504 (« délai dépassé ») au lieu d'une erreur 500.
        logger.exception("Délai dépassé lors de l'appel à Ollama")
        raise HTTPException(status_code=504, detail=TIMEOUT_MESSAGE) from error
    except (ConnectionError, ResponseError) as error:
        # Ollama est arrêté ou a renvoyé une erreur : on renvoie un code
        # 503 (« service indisponible ») au lieu d'une erreur 500.
        logger.exception("Erreur lors de l'appel à Ollama")
        raise HTTPException(
            status_code=503,
            detail=f"Service LLM indisponible : {error}",
        ) from error

    log_duration(start, history)
    return AskResponse(
        answer=result["answer"],
        sources=result["sources"],
        standalone_question=result["standalone_question"],
        search_query=result["search_query"],
        rewrites=result["rewrites"],
    )


@app.post("/ask/stream")
def ask_question_stream(
    request: AskRequest,
    agent: Annotated[CragAgent, Depends(get_agent)],
) -> StreamingResponse:
    """Pose une question et renvoie la réponse au fil de sa rédaction.

    La réponse est au format « NDJSON » : une ligne de JSON par
    événement (voir CragAgent.ask_stream) :
        {"type": "status", "step": "search"}
        {"type": "token", "text": "Le responsable"}
        {"type": "token", "text": " du traitement..."}
        {"type": "done", "answer": "...", "sources": [...], ...}

    Le code HTTP (200) est envoyé avant la réponse : une erreur
    d'Ollama arrive donc sous la forme d'un dernier événement
        {"type": "error", "status": 504, "detail": "..."}
    avec le code qu'aurait renvoyé POST /ask (503 ou 504).
    """
    history = history_to_dicts(request)

    def generate_lines():
        start = time.perf_counter()
        try:
            events = agent.ask_stream(
                request.question, request.language, history
            )
            for event in events:
                yield to_json_line(event)
        except httpx.TimeoutException:
            logger.exception("Délai dépassé lors de l'appel à Ollama")
            yield to_json_line(
                {"type": "error", "status": 504, "detail": TIMEOUT_MESSAGE}
            )
            return
        except (ConnectionError, ResponseError) as error:
            logger.exception("Erreur lors de l'appel à Ollama")
            yield to_json_line(
                {
                    "type": "error",
                    "status": 503,
                    "detail": f"Service LLM indisponible : {error}",
                }
            )
            return
        log_duration(start, history)

    return StreamingResponse(
        generate_lines(),
        media_type="application/x-ndjson",
        # Demande aux proxys (nginx...) de transmettre chaque morceau
        # tout de suite, au lieu d'attendre la fin de la réponse.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------
# Fonctions utilitaires des routes
# ---------------------------------------------------------------------


def history_to_dicts(request: AskRequest) -> list[dict]:
    """Convertit les objets Exchange en simples dictionnaires."""
    history = []
    for exchange in request.history:
        history.append(
            {"question": exchange.question, "answer": exchange.answer}
        )
    return history


def to_json_line(event: dict) -> str:
    """Transforme un événement en une ligne de JSON (format NDJSON)."""
    # ensure_ascii=False : les accents restent lisibles (« é » et non
    # « \u00e9 »).
    return json.dumps(event, ensure_ascii=False) + "\n"


def log_duration(start: float, history: list[dict]) -> None:
    """Écrit dans les logs la durée totale de traitement d'une question."""
    logger.info(
        "Question traitée en %.1f s (%d échanges d'historique)",
        time.perf_counter() - start,
        len(history),
    )
