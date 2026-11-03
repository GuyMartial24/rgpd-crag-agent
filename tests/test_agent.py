"""Tests de l'agent CRAG et de l'API.

Les tests unitaires utilisent un faux LLM et un faux moteur de
recherche : ils fonctionnent sans Ollama ni ChromaDB.

L'évaluation de bout en bout (en bas du fichier) utilise le vrai
Ollama et la vraie base. Elle n'est lancée que si la variable
d'environnement RUN_OLLAMA_TESTS vaut 1.
"""

import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from src.graph import (
    FALLBACK_ANSWERS,
    CragAgent,
    format_history,
    parse_analysis,
    parse_grade,
    parse_relevant_numbers,
)
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

# Conversation utilisée pour tester la mémoire : la relance « Et à qui ? »
# n'a de sens qu'avec l'échange précédent.
HISTORY = [
    {"question": QUESTION, "answer": "Sous 72 heures (Article 33)."},
]
FOLLOW_UP = "Et à qui faut-il la signaler ?"
STANDALONE_QUESTION = "À qui faut-il notifier une violation de données ?"


class FakeLLM:
    """Faux LLM qui répond selon le type de prompt reçu.

    - analyse : renvoie le type de question (« rgpd » par défaut) et la
      question autonome (la question elle-même s'il n'y a pas de
      conversation, sinon « standalone ») ;
    - évaluation groupée : les extraits contenant « 72 heures » sont
      pertinents (ou « grade_answer » si on veut forcer la réponse) ;
    - évaluation d'un seul extrait (secours) : même règle ;
    - reformulation : renvoie toujours la même requête ;
    - discussion hors RGPD et génération : réponses fixes.
    Tous les prompts reçus sont gardés dans self.prompts.
    """

    def __init__(
        self,
        rewrite="délai de notification d'une violation",
        standalone=STANDALONE_QUESTION,
        question_type="rgpd",
        grade_answer=None,
    ):
        self.rewrite = rewrite
        self.standalone = standalone
        self.question_type = question_type
        self.grade_answer = grade_answer
        self.prompts = []

    def invoke(self, prompt, json_mode=False):
        self.prompts.append(prompt)

        # Prompt d'analyse de la question (premier appel).
        if prompt.startswith("Tu analyses"):
            message = prompt.split("Dernier message : ")[1].split("\n")[0]
            if "Conversation précédente :\n(aucune)" in prompt:
                question = message
            else:
                question = self.standalone
            return json.dumps(
                {"type": self.question_type, "question": question}
            )

        # Évaluation groupée : on renvoie les numéros des bons extraits.
        if prompt.startswith("Tu évalues des extraits"):
            if self.grade_answer is not None:
                return self.grade_answer
            numbers = []
            extracts = prompt.split("Extraits :")[1].split("\n\n")
            for block in extracts:
                block = block.strip()
                if block.startswith("[") and "72 heures" in block:
                    numbers.append(int(block[1 : block.index("]")]))
            return json.dumps({"pertinents": numbers})

        # Évaluation d'un seul extrait (secours).
        if prompt.startswith("Tu évalues si un extrait"):
            extract = prompt.split("Extrait :")[1]
            if "72 heures" in extract:
                return '{"pertinent": "oui"}'
            return '{"pertinent": "non"}'

        # Prompt de reformulation.
        if prompt.startswith("La recherche"):
            return f"« {self.rewrite} »"

        # Message hors RGPD.
        if "ne porte pas sur le RGPD" in prompt:
            return "Bonjour, je suis Martial, assistant IA."

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
        assert "cite les articles" not in prompt


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


def test_generation_prompt_contains_persona():
    llm = FakeLLM()
    agent = CragAgent(FakeRetriever({}, default=[ARTICLE_33]), llm)

    agent.ask(QUESTION)

    generation_prompt = llm.prompts[-1]
    assert generation_prompt.startswith("Tu es Martial, Data & AI Engineer")
    # L'assistant doit rester transparent sur sa nature d'IA.
    assert "assistant d'intelligence artificielle" in generation_prompt


def test_persona_name_and_role_are_configurable():
    llm = FakeLLM()
    agent = CragAgent(
        FakeRetriever({}, default=[ARTICLE_33]),
        llm,
        name="Ada",
        role="DPO",
    )

    agent.ask(QUESTION)

    assert llm.prompts[-1].startswith("Tu es Ada, DPO")


def test_fallback_answer_follows_language():
    retriever = FakeRetriever({}, default=[ARTICLE_1])
    agent = CragAgent(retriever, FakeLLM(), max_rewrites=0)

    result = agent.ask("Breach?", language="en")

    assert result["answer"] == FALLBACK_ANSWERS["en"]


# ----- Rapidité : nombre d'appels au LLM -----


def test_rgpd_question_needs_only_three_llm_calls():
    llm = FakeLLM()
    retriever = FakeRetriever({}, default=[ARTICLE_33, ARTICLE_1])
    agent = CragAgent(retriever, llm)

    agent.ask(QUESTION)

    # 1 analyse + 1 évaluation groupée des extraits + 1 génération.
    assert len(llm.prompts) == 3


def test_unreadable_grading_falls_back_to_one_by_one():
    # Le LLM répond n'importe quoi à l'évaluation groupée.
    llm = FakeLLM(grade_answer="je ne sais pas")
    retriever = FakeRetriever({}, default=[ARTICLE_33, ARTICLE_1])
    agent = CragAgent(retriever, llm)

    result = agent.ask(QUESTION)

    # Les 2 extraits ont alors été évalués un par un.
    one_by_one = []
    for prompt in llm.prompts:
        if prompt.startswith("Tu évalues si un extrait"):
            one_by_one.append(prompt)
    assert len(one_by_one) == 2
    assert result["sources"] == ["Article 33"]


# ----- Aiguillage des messages hors RGPD -----


def test_off_topic_message_gets_a_direct_answer():
    llm = FakeLLM(question_type="autre")
    retriever = FakeRetriever({}, default=[ARTICLE_33])
    agent = CragAgent(retriever, llm)

    result = agent.ask("Bonjour, qui êtes-vous ?")

    # Aucune recherche dans le RGPD, et seulement 2 appels au LLM.
    assert retriever.queries == []
    assert len(llm.prompts) == 2
    assert result["answer"] == "Bonjour, je suis Martial, assistant IA."
    assert result["sources"] == []
    assert result["search_query"] == ""
    # La réponse garde la personnalité de l'assistant.
    assert llm.prompts[1].startswith("Tu es Martial, Data & AI Engineer")


# ----- Mémoire de conversation -----


def test_first_question_is_used_as_is():
    llm = FakeLLM()
    retriever = FakeRetriever({}, default=[ARTICLE_33])
    agent = CragAgent(retriever, llm)

    result = agent.ask(QUESTION)

    # Sans historique, l'analyse indique « (aucune) » conversation et la
    # question est cherchée telle quelle.
    assert "Conversation précédente :\n(aucune)" in llm.prompts[0]
    assert result["standalone_question"] == QUESTION
    assert retriever.queries == [QUESTION]


def test_follow_up_question_is_rewritten_with_history():
    llm = FakeLLM()
    retriever = FakeRetriever({}, default=[ARTICLE_33])
    agent = CragAgent(retriever, llm)

    result = agent.ask(FOLLOW_UP, history=HISTORY)

    # Le LLM a reçu l'historique et la relance.
    analysis_prompt = llm.prompts[0]
    assert "Utilisateur : " + QUESTION in analysis_prompt
    assert "Dernier message : " + FOLLOW_UP in analysis_prompt
    # La recherche utilise la question réécrite.
    assert result["standalone_question"] == STANDALONE_QUESTION
    assert retriever.queries == [STANDALONE_QUESTION]


def test_generation_prompt_uses_standalone_question():
    llm = FakeLLM()
    agent = CragAgent(FakeRetriever({}, default=[ARTICLE_33]), llm)

    agent.ask(FOLLOW_UP, history=HISTORY)

    # La génération reçoit la question réécrite, sans l'historique :
    # le prompt est plus court, donc plus rapide.
    generation_prompt = llm.prompts[-1]
    assert STANDALONE_QUESTION in generation_prompt
    assert "Conversation précédente" not in generation_prompt


def test_grading_prompt_uses_shortened_extracts():
    long_text = "72 heures. " + "x" * 2000
    long_article = {"text": long_text, "section": "Article 33"}
    llm = FakeLLM()
    agent = CragAgent(
        FakeRetriever({}, default=[long_article]),
        llm,
        grade_excerpt_length=100,
    )

    result = agent.ask(QUESTION)

    # L'extrait est coupé pour l'évaluation...
    grading_prompt = llm.prompts[1]
    assert "x" * 101 not in grading_prompt
    assert "…" in grading_prompt
    # ... mais il reste complet pour la rédaction de la réponse.
    assert "x" * 2000 in llm.prompts[-1]
    assert result["sources"] == ["Article 33"]


def test_only_recent_history_is_used():
    llm = FakeLLM()
    agent = CragAgent(
        FakeRetriever({}, default=[ARTICLE_33]), llm, max_history=2
    )
    history = []
    for number in range(1, 6):
        history.append(
            {"question": f"Question {number}", "answer": f"Réponse {number}"}
        )

    agent.ask(FOLLOW_UP, history=history)

    # Seuls les 2 derniers échanges (4 et 5) sont envoyés au LLM.
    analysis_prompt = llm.prompts[0]
    assert "Question 3" not in analysis_prompt
    assert "Question 4" in analysis_prompt
    assert "Question 5" in analysis_prompt


def test_long_answers_are_truncated_in_history():
    history = [{"question": "Q", "answer": "a" * 2000}]

    text = format_history(history)

    # 300 caractères conservés, suivis de « … ».
    assert "a" * 300 + "…" in text
    assert "a" * 301 not in text


# ----- Lecture des réponses JSON du LLM -----


@pytest.mark.parametrize(
    ("llm_answer", "expected"),
    [
        ('{"type": "rgpd", "question": "Q2"}', ("rgpd", "Q2")),
        ('{"type": "autre", "question": "Bonjour"}', ("autre", "Bonjour")),
        ('{"type": "Autre", "question": ""}', ("autre", "Q")),
        ('{"question": "Q2"}', ("rgpd", "Q2")),
        ("pas du JSON", ("rgpd", "Q")),
        ('["rgpd"]', ("rgpd", "Q")),
    ],
)
def test_parse_analysis(llm_answer, expected):
    # En cas de doute, la question est traitée comme une question RGPD.
    assert parse_analysis(llm_answer, "Q") == expected


@pytest.mark.parametrize(
    ("llm_answer", "expected"),
    [
        ('{"pertinents": [1, 3]}', [1, 3]),
        ('{"pertinents": []}', []),
        ('{"relevant": ["2", "4"]}', [2, 4]),
        ("[2]", [2]),
        ('{"pertinents": [1, 1, 9]}', [1]),
        ('{"pertinents": "1"}', None),
        ("{}", None),
        ("aucun", None),
    ],
)
def test_parse_relevant_numbers(llm_answer, expected):
    # 4 extraits : seuls les numéros de 1 à 4 sont valides.
    assert parse_relevant_numbers(llm_answer, 4) == expected


# ----- Lecture de la réponse de l'évaluateur (secours) -----


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


def test_cors_allows_widget_on_another_site(client):
    # Avant un POST depuis un autre site, le navigateur envoie une
    # requête OPTIONS (« preflight ») pour demander l'autorisation.
    response = client.options(
        "/ask",
        headers={
            "Origin": "https://site-client.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_ask_endpoint(client):
    retriever = FakeRetriever({}, default=[ARTICLE_33])
    use_fake_agent(CragAgent(retriever, FakeLLM()))

    response = client.post("/ask", json={"question": QUESTION})

    assert response.status_code == 200
    assert response.json() == {
        "answer": "Sous 72 heures (Article 33).",
        "sources": ["Article 33"],
        "standalone_question": QUESTION,
        "search_query": QUESTION,
        "rewrites": 0,
    }


def test_ask_endpoint_off_topic_message(client):
    retriever = FakeRetriever({}, default=[ARTICLE_33])
    use_fake_agent(CragAgent(retriever, FakeLLM(question_type="autre")))

    response = client.post("/ask", json={"question": "Merci beaucoup !"})

    assert response.status_code == 200
    assert response.json()["sources"] == []
    assert response.json()["search_query"] == ""


def test_ask_endpoint_with_history(client):
    retriever = FakeRetriever({}, default=[ARTICLE_33])
    use_fake_agent(CragAgent(retriever, FakeLLM()))

    response = client.post(
        "/ask", json={"question": FOLLOW_UP, "history": HISTORY}
    )

    assert response.status_code == 200
    assert response.json()["standalone_question"] == STANDALONE_QUESTION


def test_ask_rejects_too_long_history(client):
    use_fake_agent(None)
    history = []
    # « _ » : nom conventionnel d'une variable que l'on n'utilise pas.
    for _ in range(21):
        history.append({"question": "Q", "answer": "R"})

    response = client.post(
        "/ask", json={"question": QUESTION, "history": history}
    )

    # 20 échanges maximum par requête.
    assert response.status_code == 422


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


def test_ask_returns_504_when_ollama_is_too_slow(client):
    class SlowLLM:
        """Faux LLM qui simule un modèle trop lent (délai dépassé)."""

        def invoke(self, prompt, json_mode=False):
            raise httpx.ReadTimeout("timed out")

    retriever = FakeRetriever({}, default=[ARTICLE_33])
    use_fake_agent(CragAgent(retriever, SlowLLM()))

    response = client.post("/ask", json={"question": QUESTION})

    assert response.status_code == 504


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
