"""Tests de l'agent CRAG et de l'API.

Les tests unitaires utilisent un faux LLM et un faux moteur de
recherche : ils fonctionnent sans Ollama ni ChromaDB.

L'évaluation de bout en bout (en bas du fichier) utilise le vrai
Ollama et la vraie base. Elle n'est lancée que si la variable
d'environnement RUN_OLLAMA_TESTS vaut 1.
"""

import os

import pytest
from fastapi.testclient import TestClient

from src.graph import FALLBACK_ANSWERS, CragAgent, parse_grade
from src.main import app, get_agent

# Deux extraits utilisés dans les tests.
ARTICLE_33 = {
    "text": (
        "Notification à l'autorité de contrôle dans un délai de 72 heures."
    ),
    "section": "Article 33",
}
ARTICLE_1 = {
    "text": "Objet et objectifs du règlement.",
    "section": "Article 1",
}

QUESTION = "Combien de temps pour signaler une fuite de données ?"


class FakeLLM:
    """Faux LLM qui répond selon le type de prompt reçu.

    - évaluation : un extrait est pertinent s'il contient « 72 heures » ;
    - reformulation : renvoie toujours la même requête ;
    - génération : renvoie une réponse fixe.
    Tous les prompts reçus sont gardés dans self.prompts.
    """

    def __init__(self, rewrite="délai de notification d'une violation"):
        self.rewrite = rewrite
        self.prompts = []

    def invoke(self, prompt, json_mode=False):
        self.prompts.append(prompt)

        # Prompt d'évaluation (le seul envoyé en mode JSON).
        if json_mode:
            extract = prompt.split("Extrait :")[1]
            if "72 heures" in extract:
                return '{"pertinent": "oui"}'
            return '{"pertinent": "non"}'

        # Prompt de reformulation.
        if prompt.startswith("La recherche"):
            return f"« {self.rewrite} »"

        # Prompt de génération de la réponse.
        return "Sous 72 heures (Article 33)."


class FakeRetriever:
    """Faux moteur de recherche.

    results_by_query : résultats à renvoyer pour certaines requêtes
    default          : résultats renvoyés pour toutes les autres
    """

    def __init__(self, results_by_query, default):
        self.results_by_query = results_by_query
        self.default = default
        # On garde une trace des appels pour les vérifier dans les tests.
        self.queries = []
        self.languages = []

    def search(self, query, k, language=None):
        self.queries.append(query)
        self.languages.append(language)

        if query in self.results_by_query:
            return self.results_by_query[query][:k]
        return self.default[:k]


# ----- Comportement de l'agent -----


def test_relevant_documents_go_straight_to_generation():
    retriever = FakeRetriever({}, default=[ARTICLE_33, ARTICLE_1])
    agent = CragAgent(retriever, FakeLLM())

    result = agent.ask(QUESTION)

    assert result["answer"] == "Sous 72 heures (Article 33)."
    # Seul l'article 33 a été jugé pertinent.
    assert result["sources"] == ["Article 33"]
    # Aucune reformulation n'a été nécessaire.
    assert result["rewrites"] == 0
    assert retriever.queries == [QUESTION]


def test_irrelevant_documents_trigger_query_rewrite():
    rewritten = "délai de notification d'une violation"
    # La question d'origine ne trouve que l'article 1 (non pertinent) ;
    # la question reformulée trouve l'article 33.
    retriever = FakeRetriever({rewritten: [ARTICLE_33]}, default=[ARTICLE_1])
    agent = CragAgent(retriever, FakeLLM(rewrite=rewritten))

    result = agent.ask(QUESTION)

    assert retriever.queries == [QUESTION, rewritten]
    assert result["search_query"] == rewritten
    assert result["rewrites"] == 1
    assert result["sources"] == ["Article 33"]


def test_fallback_when_rewrites_are_exhausted():
    # Aucune recherche ne trouve d'extrait pertinent.
    retriever = FakeRetriever({}, default=[ARTICLE_1])
    llm = FakeLLM()
    agent = CragAgent(retriever, llm, max_rewrites=2)

    result = agent.ask(QUESTION)

    assert result["answer"] == FALLBACK_ANSWERS["fr"]
    assert result["sources"] == []
    assert result["rewrites"] == 2
    # 1 recherche initiale + 2 recherches après reformulation.
    assert len(retriever.queries) == 3
    # Le LLM n'a jamais été appelé pour rédiger une réponse.
    for prompt in llm.prompts:
        assert not prompt.startswith("Tu es un assistant")


def test_generation_prompt_contains_only_relevant_extracts():
    llm = FakeLLM()
    retriever = FakeRetriever({}, default=[ARTICLE_33, ARTICLE_1])
    agent = CragAgent(retriever, llm)

    agent.ask(QUESTION)

    # Le dernier prompt envoyé au LLM est celui de la génération.
    generation_prompt = llm.prompts[-1]
    assert "[Article 33]" in generation_prompt
    assert "[Article 1]" not in generation_prompt
    assert QUESTION in generation_prompt


def test_language_is_used_for_search_and_answer():
    retriever = FakeRetriever({}, default=[ARTICLE_33])
    llm = FakeLLM()
    agent = CragAgent(retriever, llm)

    agent.ask("How long to report a breach?", language="en")

    assert retriever.languages == ["en"]
    assert "Réponds en anglais" in llm.prompts[-1]


def test_fallback_answer_follows_language():
    retriever = FakeRetriever({}, default=[ARTICLE_1])
    agent = CragAgent(retriever, FakeLLM(), max_rewrites=0)

    result = agent.ask("Breach?", language="en")

    assert result["answer"] == FALLBACK_ANSWERS["en"]


# ----- Lecture de la réponse de l'évaluateur -----


@pytest.mark.parametrize(
    ("llm_answer", "expected"),
    [
        ('{"pertinent": "oui"}', True),
        ('{"pertinent": "Oui."}', True),
        ('{"pertinent": "non"}', False),
        ('{"relevant": "yes"}', True),
        ('{"pertinent": true}', True),
        ("oui", True),
        ("Non, l'extrait ne traite pas du sujet", False),
        ("", False),
        ("{}", False),
    ],
)
def test_parse_grade(llm_answer, expected):
    assert parse_grade(llm_answer) is expected


# ----- API FastAPI -----


@pytest.fixture
def client():
    """Client de test de l'API. Les remplacements sont annulés après."""
    yield TestClient(app)
    app.dependency_overrides.clear()


def use_fake_agent(agent):
    """Remplace le vrai agent par « agent » dans l'API."""
    app.dependency_overrides[get_agent] = lambda: agent


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ask_endpoint(client):
    retriever = FakeRetriever({}, default=[ARTICLE_33])
    use_fake_agent(CragAgent(retriever, FakeLLM()))

    response = client.post("/ask", json={"question": QUESTION})

    assert response.status_code == 200
    assert response.json() == {
        "answer": "Sous 72 heures (Article 33).",
        "sources": ["Article 33"],
        "search_query": QUESTION,
        "rewrites": 0,
    }


def test_ask_rejects_invalid_requests(client):
    use_fake_agent(None)

    # Question vide : refusée (code 422 = données invalides).
    response = client.post("/ask", json={"question": ""})
    assert response.status_code == 422

    # Langue non prise en charge : refusée.
    response = client.post(
        "/ask", json={"question": QUESTION, "language": "de"}
    )
    assert response.status_code == 422


def test_ask_returns_503_when_ollama_is_down(client):
    class BrokenLLM:
        """Faux LLM qui simule un serveur Ollama arrêté."""

        def invoke(self, prompt, json_mode=False):
            raise ConnectionError("Failed to connect to Ollama")

    retriever = FakeRetriever({}, default=[ARTICLE_33])
    use_fake_agent(CragAgent(retriever, BrokenLLM()))

    response = client.post("/ask", json={"question": QUESTION})

    assert response.status_code == 503


# ----- Évaluation de bout en bout (nécessite Ollama) -----

# Questions de référence : (question, langue, article attendu).
EVAL_SET = [
    (
        "Dans quel délai notifier une violation de données à la CNIL ?",
        "fr",
        "Article 33",
    ),
    (
        "Quelles sont les bases légales permettant de traiter des données ?",
        "fr",
        "Article 6",
    ),
    (
        "Puis-je demander la suppression de mes données personnelles ?",
        "fr",
        "Article 17",
    ),
    ("Quand faut-il réaliser une analyse d'impact ?", "fr", "Article 35"),
    ("Quel est le montant maximal des amendes ?", "fr", "Article 83"),
    (
        "When must a data breach be notified to the supervisory authority?",
        "en",
        "Article 33",
    ),
    ("Can I ask a company to erase my personal data?", "en", "Article 17"),
]


@pytest.mark.skipif(
    os.getenv("RUN_OLLAMA_TESTS") != "1",
    reason="nécessite Ollama et un index ChromaDB",
)
@pytest.mark.parametrize(("question", "language", "expected"), EVAL_SET)
def test_end_to_end_evaluation(question, language, expected):
    from src.graph import create_default_agent

    agent = create_default_agent()
    result = agent.ask(question, language)

    assert expected in result["sources"], result
    assert result["answer"] != FALLBACK_ANSWERS[language]
