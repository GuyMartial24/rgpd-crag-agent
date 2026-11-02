"""Agent Corrective RAG (CRAG) construit avec LangGraph.

Un RAG classique répond à partir des extraits trouvés, même s'ils ne
parlent pas du sujet. Le CRAG ajoute une étape de vérification :

    1. retrieve        : on cherche des extraits du RGPD dans ChromaDB ;
    2. grade_documents : le LLM juge si chaque extrait est pertinent ;
    3. ensuite, trois cas possibles :
       - au moins un extrait pertinent  -> generate (on répond) ;
       - aucun, mais essais restants     -> transform_query (on
         reformule la question, puis on retourne à l'étape 1) ;
       - aucun, et plus d'essais         -> fallback (on avoue ne pas
         savoir plutôt que d'inventer une réponse).
"""

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from ollama import Client

from src.config import Settings, get_settings

# ---------------------------------------------------------------------
# Prompts envoyés au LLM
# Les doubles accolades {{ }} affichent une accolade simple : elles
# évitent que .format() les prenne pour une variable à remplacer.
# Le « \ » en fin de ligne colle la ligne suivante (pas de retour à la
# ligne dans le texte) : il permet de respecter 79 caractères par ligne.
# ---------------------------------------------------------------------

GRADE_PROMPT = """Tu évalues si un extrait du RGPD aide à répondre à une \
question.

Question : {question}

Extrait :
{document}

L'extrait est pertinent s'il contient des informations utiles pour \
répondre, même partiellement.
Réponds uniquement en JSON : {{"pertinent": "oui"}} ou \
{{"pertinent": "non"}}."""

REWRITE_PROMPT = """La recherche dans le texte du RGPD n'a pas donné de \
résultat pertinent.

Question initiale : {question}
Dernière requête utilisée : {query}

Reformule la requête en {language_name} pour retrouver les articles du \
RGPD qui y répondent, en employant le vocabulaire du règlement \
(« responsable du traitement » / "controller", « personne concernée » / \
"data subject", « autorité de contrôle » / "supervisory authority"...).
Réponds uniquement par la nouvelle requête, sans commentaire."""

GENERATE_PROMPT = """Tu es un assistant spécialisé dans le Règlement \
général sur la protection des données (RGPD). Réponds en {language_name} \
à la question en t'appuyant uniquement sur les extraits ci-dessous et \
cite les articles ou considérants utilisés. Si les extraits ne \
permettent pas de répondre complètement, indique-le.

Extraits :
{context}

Question : {question}

Réponse :"""

# Nom de chaque langue, inséré dans les prompts.
LANGUAGE_NAMES = {"fr": "français", "en": "anglais"}

# Réponse renvoyée quand aucun extrait pertinent n'a été trouvé.
FALLBACK_ANSWERS = {
    "fr": (
        "Je n'ai pas trouvé dans le RGPD d'extrait suffisamment pertinent "
        "pour répondre de manière fiable à cette question. Essayez de la "
        "reformuler ou de préciser l'article visé."
    ),
    "en": (
        "I could not find a sufficiently relevant passage of the GDPR to "
        "answer this question reliably. Try rephrasing it or specifying "
        "the article concerned."
    ),
}


class AgentState(TypedDict, total=False):
    """Données partagées entre les étapes du graphe.

    Chaque étape lit l'état et renvoie un dictionnaire contenant
    uniquement les clés qu'elle modifie. LangGraph s'occupe ensuite de
    mettre à jour l'état.
    """

    question: str  # question posée par l'utilisateur
    language: str  # « fr » ou « en »
    search_query: str  # requête utilisée pour la recherche
    documents: list[dict]  # extraits trouvés par la recherche
    relevant_documents: list[dict]  # extraits jugés pertinents
    rewrites: int  # nombre de reformulations déjà faites
    answer: str  # réponse finale


class OllamaLLM:
    """Petit client pour interroger un modèle de langage via Ollama."""

    def __init__(self, settings: Settings):
        self.client = Client(
            host=settings.ollama_base_url,
            timeout=settings.request_timeout,
        )
        self.model = settings.llm_model
        self.temperature = settings.llm_temperature

    def invoke(self, prompt: str, json_mode: bool = False) -> str:
        """Envoie le prompt au modèle et retourne sa réponse (texte).

        Avec json_mode=True, Ollama force le modèle à répondre en JSON.
        """
        if json_mode:
            response_format = "json"
        else:
            response_format = None

        response = self.client.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            format=response_format,
            options={"temperature": self.temperature},
        )

        if response.message.content is None:
            return ""
        return response.message.content


def parse_grade(llm_answer: str) -> bool:
    """Transforme la réponse de l'évaluateur en booléen.

    Le LLM doit répondre en JSON, par exemple {"pertinent": "oui"}, mais
    on reste tolérant : on accepte aussi du texte simple. Tout ce qui ne
    ressemble pas à « oui » est considéré comme « non pertinent ».
    """
    verdict = llm_answer

    # Si la réponse est du JSON valide, on récupère la valeur.
    try:
        data = json.loads(llm_answer)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        # On prend la première valeur, quel que soit le nom de la clé.
        values = list(data.values())
        if len(values) > 0:
            verdict = values[0]
        else:
            verdict = ""
    elif isinstance(data, bool):
        verdict = data

    # Cas où le LLM a répondu directement true / false.
    if isinstance(verdict, bool):
        return verdict

    verdict = str(verdict).strip().lower()
    return (
        verdict.startswith("oui")
        or verdict.startswith("yes")
        or verdict.startswith("true")
    )


def format_context(documents: list[dict]) -> str:
    """Met les extraits bout à bout pour les insérer dans le prompt."""
    parts = []
    for document in documents:
        parts.append(f"[{document['section']}]\n{document['text']}")
    return "\n\n".join(parts)


class CragAgent:
    """Agent CRAG : recherche, vérification, reformulation et réponse.

    Paramètres :
        retriever    : objet ayant une méthode search(query, k, language)
        llm          : objet ayant une méthode invoke(prompt, json_mode)
        top_k        : nombre d'extraits récupérés par recherche
        max_rewrites : nombre maximum de reformulations
    """

    def __init__(self, retriever, llm, top_k=4, max_rewrites=2):
        self.retriever = retriever
        self.llm = llm
        self.top_k = top_k
        self.max_rewrites = max_rewrites
        self.graph = self.build_graph()

    def build_graph(self):
        """Assemble les étapes de l'agent dans un graphe LangGraph."""
        workflow = StateGraph(AgentState)

        # Chaque nœud est une étape de l'agent.
        workflow.add_node("retrieve", self.retrieve)
        workflow.add_node("grade_documents", self.grade_documents)
        workflow.add_node("transform_query", self.transform_query)
        workflow.add_node("generate", self.generate)
        workflow.add_node("fallback", self.fallback)

        # Les arêtes indiquent l'ordre des étapes.
        workflow.add_edge(START, "retrieve")
        workflow.add_edge("retrieve", "grade_documents")

        # Après l'évaluation, decide_next_step choisit l'étape suivante.
        workflow.add_conditional_edges(
            "grade_documents",
            self.decide_next_step,
            {
                "generate": "generate",
                "transform_query": "transform_query",
                "fallback": "fallback",
            },
        )

        # Après une reformulation, on relance une recherche (boucle).
        workflow.add_edge("transform_query", "retrieve")
        workflow.add_edge("generate", END)
        workflow.add_edge("fallback", END)

        return workflow.compile()

    # ----- Étapes du graphe -----

    def retrieve(self, state: AgentState) -> dict:
        """Étape 1 : cherche des extraits du RGPD."""
        # Au premier passage, on cherche avec la question de
        # l'utilisateur. Ensuite, avec la question reformulée.
        query = state.get("search_query")
        if not query:
            query = state["question"]

        documents = self.retriever.search(query, self.top_k, state["language"])
        return {"search_query": query, "documents": documents}

    def grade_documents(self, state: AgentState) -> dict:
        """Étape 2 : garde uniquement les extraits jugés pertinents."""
        relevant_documents = []

        for document in state["documents"]:
            prompt = GRADE_PROMPT.format(
                question=state["question"],
                document=document["text"],
            )
            llm_answer = self.llm.invoke(prompt, json_mode=True)

            if parse_grade(llm_answer):
                relevant_documents.append(document)

        return {"relevant_documents": relevant_documents}

    def decide_next_step(self, state: AgentState) -> str:
        """Choisit l'étape suivante après l'évaluation des extraits."""
        if len(state["relevant_documents"]) > 0:
            return "generate"

        if state.get("rewrites", 0) < self.max_rewrites:
            return "transform_query"

        return "fallback"

    def transform_query(self, state: AgentState) -> dict:
        """Étape 3a : demande au LLM de reformuler la requête."""
        prompt = REWRITE_PROMPT.format(
            question=state["question"],
            query=state["search_query"],
            language_name=LANGUAGE_NAMES[state["language"]],
        )
        new_query = self.llm.invoke(prompt)

        # Le LLM entoure parfois sa réponse de guillemets : on les retire.
        new_query = new_query.strip().strip("\"'« »")

        # Si le LLM ne renvoie rien, on garde l'ancienne requête.
        if new_query == "":
            new_query = state["search_query"]

        return {
            "search_query": new_query,
            "rewrites": state.get("rewrites", 0) + 1,
        }

    def generate(self, state: AgentState) -> dict:
        """Étape 3b : rédige la réponse à partir des extraits pertinents."""
        prompt = GENERATE_PROMPT.format(
            context=format_context(state["relevant_documents"]),
            question=state["question"],
            language_name=LANGUAGE_NAMES[state["language"]],
        )
        answer = self.llm.invoke(prompt)
        return {"answer": answer.strip()}

    def fallback(self, state: AgentState) -> dict:
        """Étape 3c : aucun extrait pertinent, on le dit honnêtement."""
        return {"answer": FALLBACK_ANSWERS[state["language"]]}

    # ----- Utilisation de l'agent -----

    def ask(self, question: str, language: str = "fr") -> dict:
        """Pose une question à l'agent et retourne sa réponse.

        Le dictionnaire retourné contient :
            answer       : la réponse rédigée
            sources      : les articles/considérants utilisés
            search_query : la dernière requête de recherche
            rewrites     : le nombre de reformulations effectuées
        """
        initial_state = {
            "question": question,
            "language": language,
            "rewrites": 0,
        }
        final_state = self.graph.invoke(initial_state)

        # Liste des sources, sans doublon et dans l'ordre d'apparition.
        sources = []
        for document in final_state.get("relevant_documents", []):
            if document["section"] not in sources:
                sources.append(document["section"])

        return {
            "answer": final_state["answer"],
            "sources": sources,
            "search_query": final_state["search_query"],
            "rewrites": final_state.get("rewrites", 0),
        }


def create_default_agent(settings=None) -> CragAgent:
    """Crée l'agent avec Ollama et la base ChromaDB du projet."""
    # Import placé ici pour ne charger ChromaDB que lorsqu'on en a besoin
    # (les tests de l'agent n'en ont pas besoin).
    from src.ingest import ChromaRetriever, OllamaEmbedder, get_collection

    if settings is None:
        settings = get_settings()

    retriever = ChromaRetriever(
        get_collection(settings), OllamaEmbedder(settings)
    )
    llm = OllamaLLM(settings)
    return CragAgent(retriever, llm, settings.top_k, settings.max_rewrites)
