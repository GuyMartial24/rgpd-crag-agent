"""API REST (FastAPI) qui expose l'agent CRAG RGPD.

Lancement en local :
    uvicorn src.main:app --reload

Documentation interactive : http://localhost:8000/docs
"""

import logging
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException
from ollama import ResponseError
from pydantic import BaseModel, Field

from src import __version__
from src.graph import CragAgent, create_default_agent

logger = logging.getLogger(__name__)

app = FastAPI(
    title="RGPD CRAG Agent",
    description=(
        "Questions-réponses sur le RGPD via un agent Corrective RAG "
        "(LangGraph + Ollama)."
    ),
    version=__version__,
)


# ---------------------------------------------------------------------
# Modèles de données (Pydantic)
# Ils décrivent le format des requêtes et des réponses. FastAPI les
# utilise pour valider les données reçues et générer la documentation.
# ---------------------------------------------------------------------


class AskRequest(BaseModel):
    """Corps de la requête POST /ask."""

    # La question doit contenir entre 3 et 1000 caractères.
    question: str = Field(
        min_length=3,
        max_length=1000,
        examples=["Quand une AIPD est-elle obligatoire ?"],
    )
    # Seules les valeurs « fr » et « en » sont acceptées.
    language: Literal["fr", "en"] = Field(
        default="fr",
        description="Langue du texte du RGPD interrogé et de la réponse",
    )


class AskResponse(BaseModel):
    """Réponse renvoyée par POST /ask."""

    answer: str
    sources: list[str]
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
    """Pose une question sur le RGPD à l'agent."""
    try:
        result = agent.ask(request.question, request.language)
    except (ConnectionError, ResponseError) as error:
        # Ollama est arrêté ou a renvoyé une erreur : on renvoie un code
        # 503 (« service indisponible ») au lieu d'une erreur 500.
        logger.exception("Erreur lors de l'appel à Ollama")
        raise HTTPException(
            status_code=503,
            detail=f"Service LLM indisponible : {error}",
        ) from error

    return AskResponse(
        answer=result["answer"],
        sources=result["sources"],
        search_query=result["search_query"],
        rewrites=result["rewrites"],
    )
