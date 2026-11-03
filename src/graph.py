"""Agent Corrective RAG (CRAG) construit avec LangGraph.

Un RAG classique répond à partir des extraits trouvés, même s'ils ne
parlent pas du sujet. Le CRAG ajoute une étape de vérification :

    0. analyze_question : en un seul appel au LLM, on détermine si la
                          question porte sur le RGPD et on la réécrit
                          pour qu'elle soit compréhensible seule
                          (ex. « et pour les mineurs ? » devient
                          « Quelles sont les conditions du consentement
                          pour les mineurs ? ») ;
       - question hors RGPD (« bonjour », « qui es-tu ? »)
                                        -> prepare_chat (réponse courte,
                                           sans recherche) ;
    1. retrieve         : on cherche des extraits du RGPD dans ChromaDB ;
    2. grade_documents  : le LLM juge, en un seul appel, quels extraits
                          sont pertinents ;
    3. ensuite, trois cas possibles :
       - au moins un extrait pertinent  -> prepare_answer (on prépare
         le prompt de la réponse) ;
       - aucun, mais essais restants     -> transform_query (on
         reformule la question, puis on retourne à l'étape 1) ;
       - aucun, et plus d'essais         -> fallback (on avoue ne pas
         savoir plutôt que d'inventer une réponse).
    4. rédaction de la réponse : elle a lieu après le graphe, pour
       pouvoir envoyer la réponse morceau par morceau (« streaming »)
       pendant que le LLM l'écrit.

Mémoire de conversation : l'API ne stocke rien. C'est le client (le
widget de chat) qui renvoie l'historique des échanges à chaque question.
"""

import json
import logging
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from ollama import Client

from src.config import Settings, get_settings
from src.vocabulary import expand_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Prompts envoyés au LLM
# Les doubles accolades {{ }} affichent une accolade simple : elles
# évitent que .format() les prenne pour une variable à remplacer.
# Le « \ » en fin de ligne colle la ligne suivante (pas de retour à la
# ligne dans le texte) : il permet de respecter 79 caractères par ligne.
# ---------------------------------------------------------------------

# Les exemples sont importants : un petit modèle (3 milliards de
# paramètres) suit beaucoup mieux des exemples concrets qu'une règle
# abstraite. Sans eux, il recopiait les relances telles quelles
# (« Et à qui faut-il la signaler ? ») et la recherche perdait le sujet.
ANALYZE_PROMPT = """Tu analyses le dernier message d'un utilisateur qui \
discute avec un assistant spécialisé dans le RGPD.

Donne deux informations :
1. « type » : « rgpd » si le message pose une question sur le RGPD, \
les données personnelles, la vie privée, la CNIL ou les obligations \
d'une organisation ; « autre » pour une salutation, un remerciement, \
une question sur l'assistant lui-même ou un sujet sans rapport.
2. « question » : le message réécrit en {language_name} sous la forme \
d'une question complète, compréhensible SANS la conversation. Si le \
message fait référence à la conversation (« et pour... », « et à \
qui... », un pronom comme « la », « le », « les », « il », « elle »), \
remplace cette référence par le sujet dont on parlait. Si le message \
est déjà complet, recopie-le.

Exemples :
- Conversation : « Quand le consentement est-il valable ? »
  Message : « Et pour les mineurs ? »
  Réponse : {{"type": "rgpd", "question": "Quand le consentement d'un \
mineur est-il valable ?"}}
- Conversation : « Dans quel délai notifier une violation de données ? »
  Message : « Et qui doit la notifier ? »
  Réponse : {{"type": "rgpd", "question": "Qui doit notifier une \
violation de données ?"}}
- Conversation : « Qu'est-ce qu'une donnée sensible ? »
  Message : « Merci, c'est très clair ! »
  Réponse : {{"type": "autre", "question": "Merci, c'est très clair !"}}

À toi :
Conversation précédente :
{history}

Dernier message : {question}

Réponds uniquement en JSON : {{"type": "...", "question": "..."}}"""

# Mots qui, en début de message, signalent presque toujours une relance
# (« Et pour les mineurs ? », « Mais qui doit le faire ? »).
FOLLOW_UP_STARTS = (
    "et ",
    "mais ",
    "alors ",
    "aussi ",
    "and ",
    "but ",
    "what about ",
    "how about ",
)

GRADE_PROMPT = """Tu évalues des extraits du RGPD pour répondre à une \
question.

Question : {question}

Extraits :
{documents}

Un extrait est pertinent s'il contient des informations utiles pour \
répondre, même partiellement. Le RGPD pose surtout des règles \
générales : un extrait qui énonce un principe applicable à la situation \
est pertinent, même s'il ne cite pas le cas précis (employeur, client, \
site web...).
Réponds uniquement en JSON avec la liste des numéros des extraits \
pertinents, par exemple {{"pertinents": [1, 3]}}, ou \
{{"pertinents": []}} si aucun ne l'est."""

# Évaluation d'un seul extrait : utilisée seulement en secours, si la
# réponse à l'évaluation groupée est illisible.
GRADE_ONE_PROMPT = """Tu évalues si un extrait du RGPD aide à répondre à \
une question.

Question : {question}

Extrait :
{document}

L'extrait est pertinent s'il contient des informations utiles pour \
répondre, même partiellement. Un extrait qui énonce un principe \
général applicable à la situation est pertinent, même s'il ne cite pas \
le cas précis.
Réponds uniquement en JSON : {{"pertinent": "oui"}} ou \
{{"pertinent": "non"}}."""

REWRITE_PROMPT = """La recherche dans le texte du RGPD n'a pas donné de \
résultat pertinent.

Question initiale : {question}
Dernière requête utilisée : {query}

Reformule la requête en {language_name} pour retrouver les articles du \
RGPD qui y répondent :
- emploie le vocabulaire du règlement, par exemple :
  « responsable du traitement » / "controller" (pour une entreprise, \
un employeur, un site web) ;
  « personne concernée » / "data subject" (pour un client, un salarié, \
un utilisateur) ;
  « violation de données à caractère personnel » / "personal data \
breach" (pour une fuite, un piratage) ;
  « limitation de la conservation » / "storage limitation" (pour une \
durée de conservation) ;
  « autorité de contrôle » / "supervisory authority" (pour la CNIL) ;
- formule une question générale : le RGPD pose surtout des principes \
communs à tous les secteurs.
Réponds uniquement par la nouvelle requête, sans commentaire."""

# Personnalité de l'assistant, placée en tête des prompts de réponse.
# Le nom et le titre viennent de la configuration (ASSISTANT_NAME et
# ASSISTANT_ROLE). L'assistant reste transparent sur sa nature d'IA :
# l'utilisateur doit savoir qu'il ne parle pas à un humain.
PERSONA = """Tu es {name}, {role}, spécialisé dans la protection des \
données personnelles. Tu es un assistant d'intelligence artificielle : \
si on te demande qui tu es, présente-toi comme l'assistant IA {name}, \
sans jamais prétendre être une personne humaine.
Ton style : clair, pédagogique et professionnel, avec une touche de \
bienveillance. Tu vouvoies ton interlocuteur. Tu vas droit au but, puis \
tu donnes un exemple concret quand c'est utile."""

GENERATE_PROMPT = """{persona}

Réponds en {language_name} à la question sur le Règlement général sur la \
protection des données (RGPD) en t'appuyant uniquement sur les extraits \
ci-dessous et cite les articles ou considérants utilisés. Si les \
extraits ne permettent pas de répondre complètement, indique-le.
Sois concis : 150 mots au maximum, en allant à l'essentiel.

Extraits :
{context}

Question : {question}

Réponse :"""

# Réponse aux messages qui ne portent pas sur le RGPD (salutation,
# remerciement, « qui es-tu ? »...) : aucune recherche n'est faite.
CHAT_PROMPT = """{persona}

L'utilisateur t'écrit un message qui ne porte pas sur le RGPD \
(salutation, remerciement, question sur toi...).
{history_block}
Message : {question}

Réponds en {language_name}, en deux ou trois phrases au maximum : \
réponds simplement et poliment, puis rappelle que tu es là pour \
répondre aux questions sur le RGPD et la protection des données."""

# Bloc ajouté aux prompts de réponse quand une conversation est en
# cours, pour que la réponse reste cohérente avec les échanges passés.
HISTORY_BLOCK = """
Conversation précédente (pour le contexte uniquement) :
{history}
"""

# Nom de chaque langue, inséré dans les prompts.
LANGUAGE_NAMES = {"fr": "français", "en": "anglais"}

# Réponse renvoyée quand aucun extrait pertinent n'a été trouvé.
FALLBACK_ANSWERS = {
    "fr": (
        "Je n'ai pas trouvé dans le RGPD de passage suffisamment pertinent "
        "pour vous répondre de manière fiable. Pourriez-vous reformuler "
        "votre question ou préciser l'article qui vous intéresse ?"
    ),
    "en": (
        "I could not find a sufficiently relevant passage of the GDPR to "
        "answer you reliably. Could you rephrase your question or specify "
        "the article you are interested in?"
    ),
}

# Longueur maximale (en caractères) d'une réponse passée recopiée dans
# les prompts : l'historique reste court et le LLM reste rapide.
MAX_ANSWER_LENGTH_IN_HISTORY = 300

# Nombre maximum de tokens des réponses JSON (analyse, évaluation) :
# elles ne font que quelques mots, inutile de laisser le LLM continuer.
MAX_JSON_TOKENS = 200


class AgentState(TypedDict, total=False):
    """Données partagées entre les étapes du graphe.

    Chaque étape lit l'état et renvoie un dictionnaire contenant
    uniquement les clés qu'elle modifie. LangGraph s'occupe ensuite de
    mettre à jour l'état.
    """

    question: str  # question posée par l'utilisateur
    language: str  # « fr » ou « en »
    history: list[dict]  # échanges précédents {"question", "answer"}
    question_type: str  # « rgpd » ou « autre »
    standalone_question: str  # question compréhensible sans l'historique
    search_query: str  # requête utilisée pour la recherche
    documents: list[dict]  # extraits trouvés par la recherche
    relevant_documents: list[dict]  # extraits jugés pertinents
    rewrites: int  # nombre de reformulations déjà faites
    answer_prompt: str  # prompt à envoyer au LLM pour rédiger la réponse
    answer: str  # réponse fixe (cas « fallback », sans appel au LLM)


class OllamaLLM:
    """Petit client pour interroger un modèle de langage via Ollama."""

    def __init__(self, settings: Settings):
        self.client = Client(
            host=settings.ollama_base_url,
            timeout=settings.request_timeout,
        )
        self.model = settings.llm_model
        self.temperature = settings.llm_temperature
        self.max_answer_tokens = settings.max_answer_tokens
        self.keep_alive = settings.ollama_keep_alive

    def invoke(self, prompt: str, json_mode: bool = False) -> str:
        """Envoie le prompt au modèle et retourne sa réponse (texte).

        Avec json_mode=True, Ollama force le modèle à répondre en JSON.
        """
        if json_mode:
            response_format = "json"
            max_tokens = MAX_JSON_TOKENS
        else:
            response_format = None
            max_tokens = self.max_answer_tokens

        response = self.client.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            format=response_format,
            options=self.make_options(max_tokens),
            keep_alive=self.keep_alive,
        )
        log_timings(response)

        if response.message.content is None:
            return ""
        return response.message.content

    def stream(self, prompt: str):
        """Envoie le prompt et renvoie la réponse morceau par morceau.

        C'est un « générateur » (mot-clé yield) : on l'utilise dans une
        boucle for, et chaque tour de boucle donne le morceau de texte
        suivant dès que le LLM l'a écrit.
        """
        parts = self.client.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            options=self.make_options(self.max_answer_tokens),
            keep_alive=self.keep_alive,
            stream=True,
        )
        for part in parts:
            if part.message.content:
                yield part.message.content
            # Le dernier morceau contient les durées de l'appel.
            if part.done:
                log_timings(part)

    def make_options(self, max_tokens: int) -> dict:
        """Options du modèle communes à tous les appels.

        Remarque : on ne change pas la taille du contexte (num_ctx) :
        si le modèle est partagé avec d'autres applications, une autre
        valeur obligerait Ollama à le recharger.
        """
        return {"temperature": self.temperature, "num_predict": max_tokens}


def log_timings(response) -> None:
    """Écrit dans les logs le temps passé par Ollama sur un appel.

    Ollama donne les durées en nanosecondes : on les convertit en
    secondes. Ces chiffres montrent où part le temps :
        chargement : le modèle n'était pas en mémoire ;
        lecture    : traitement du prompt (proportionnel à sa longueur) ;
        écriture   : rédaction de la réponse.
    """

    def seconds(nanoseconds):
        if nanoseconds is None:
            return 0.0
        return nanoseconds / 1_000_000_000

    logger.info(
        "Ollama : %.1f s au total (chargement %.1f s, lecture de %s tokens "
        "en %.1f s, écriture de %s tokens en %.1f s)",
        seconds(response.total_duration),
        seconds(response.load_duration),
        response.prompt_eval_count,
        seconds(response.prompt_eval_duration),
        response.eval_count,
        seconds(response.eval_duration),
    )


# ---------------------------------------------------------------------
# Lecture des réponses JSON du LLM
# Un LLM ne respecte pas toujours parfaitement le format demandé : ces
# fonctions sont tolérantes et prévoient une valeur par défaut sûre.
# ---------------------------------------------------------------------


def parse_analysis(llm_answer: str, question: str) -> tuple[str, str]:
    """Lit la réponse de l'analyse : (type, question autonome).

    En cas de réponse illisible, on considère que la question porte sur
    le RGPD et on la garde telle quelle : c'est le choix le plus sûr.
    """
    question_type = "rgpd"
    standalone_question = question

    try:
        data = json.loads(llm_answer)
    except json.JSONDecodeError:
        return question_type, standalone_question

    if not isinstance(data, dict):
        return question_type, standalone_question

    if str(data.get("type", "")).strip().lower() == "autre":
        question_type = "autre"

    rewritten = str(data.get("question", "")).strip().strip("\"'« »")
    if rewritten != "":
        standalone_question = rewritten

    return question_type, standalone_question


def add_missing_context(
    question: str, standalone_question: str, history: list[dict]
) -> str:
    """Filet de sécurité pour les relances que le LLM n'a pas réécrites.

    Si le message commence comme une relance (« Et à qui... ? ») et que
    le LLM l'a renvoyé sans le changer, on le fait précéder de la
    question précédente. La recherche dans le RGPD garde ainsi le sujet
    de la conversation, par exemple :
        « Dans quel délai notifier une violation ? Et à qui ? »
    """
    if len(history) == 0:
        return standalone_question

    unchanged = standalone_question.strip().lower() == question.strip().lower()
    looks_like_follow_up = (
        question.strip().lower().startswith(FOLLOW_UP_STARTS)
    )
    if not unchanged or not looks_like_follow_up:
        return standalone_question

    previous_question = history[-1]["question"]
    return previous_question + " " + question


def parse_relevant_numbers(llm_answer: str, count: int):
    """Lit les numéros des extraits pertinents (de 1 à count).

    Retourne une liste de numéros, ou None si la réponse est illisible
    (l'agent évalue alors les extraits un par un).
    """
    try:
        data = json.loads(llm_answer)
    except json.JSONDecodeError:
        return None

    # Réponse attendue : {"pertinents": [1, 3]}. On accepte aussi une
    # clé portant un autre nom, ou directement une liste [1, 3].
    if isinstance(data, dict):
        values = list(data.values())
        if len(values) == 0:
            return None
        data = values[0]

    if not isinstance(data, list):
        return None

    numbers = []
    for item in data:
        # Le LLM écrit parfois les numéros entre guillemets : "1".
        if isinstance(item, str) and item.strip().isdigit():
            item = int(item.strip())
        if isinstance(item, int) and 1 <= item <= count:
            if item not in numbers:
                numbers.append(item)
    return numbers


def parse_grade(llm_answer: str) -> bool:
    """Transforme la réponse de l'évaluation d'un extrait en booléen.

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


# ---------------------------------------------------------------------
# Mise en forme des textes insérés dans les prompts
# ---------------------------------------------------------------------


def format_context(documents: list[dict]) -> str:
    """Met les extraits bout à bout pour le prompt de génération."""
    parts = []
    for document in documents:
        parts.append(f"[{document['section']}]\n{document['text']}")
    return "\n\n".join(parts)


def shorten(text: str, max_length: int) -> str:
    """Coupe un texte trop long et ajoute « … » à la fin."""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "…"


def format_numbered_documents(documents: list[dict], max_length: int) -> str:
    """Numérote les extraits ([1], [2]...) pour l'évaluation groupée.

    Chaque extrait est raccourci à max_length caractères : le début
    suffit pour juger s'il est pertinent.
    """
    parts = []
    for number, document in enumerate(documents, start=1):
        text = shorten(document["text"], max_length)
        parts.append(f"[{number}] {text}")
    return "\n\n".join(parts)


def format_history(history: list[dict]) -> str:
    """Transforme l'historique en texte lisible pour le LLM.

    Les réponses trop longues sont tronquées pour garder un prompt court.
    """
    lines = []
    for exchange in history:
        answer = shorten(exchange["answer"], MAX_ANSWER_LENGTH_IN_HISTORY)
        lines.append("Utilisateur : " + exchange["question"])
        lines.append("Assistant : " + answer)
    return "\n".join(lines)


def make_history_block(history: list[dict]) -> str:
    """Bloc « conversation précédente » des prompts de réponse."""
    if len(history) == 0:
        return ""
    return HISTORY_BLOCK.format(history=format_history(history))


def timed(step):
    """Enveloppe une étape du graphe pour mesurer sa durée.

    La fonction renvoyée fait la même chose que l'étape, puis écrit dans
    les logs son nom et le temps qu'elle a pris.
    """

    def run_and_measure(state):
        start = time.perf_counter()
        result = step(state)
        duration = time.perf_counter() - start
        logger.info("Étape %s : %.1f s", step.__name__, duration)
        return result

    return run_and_measure


class CragAgent:
    """Agent CRAG : recherche, vérification, reformulation et réponse.

    Paramètres :
        retriever    : objet ayant une méthode search(query, k, language)
        llm          : objet ayant une méthode invoke(prompt, json_mode)
                       et une méthode stream(prompt)
        top_k        : nombre d'extraits récupérés par recherche
        max_rewrites : nombre maximum de reformulations
        max_history  : nombre maximum d'échanges passés pris en compte
        name, role   : nom et titre de l'assistant (sa personnalité)
        grade_excerpt_length : longueur des extraits pour l'évaluation
    """

    def __init__(
        self,
        retriever,
        llm,
        top_k=3,
        max_rewrites=1,
        max_history=4,
        name="Martial",
        role="Assistant RGPD",
        grade_excerpt_length=400,
    ):
        self.retriever = retriever
        self.llm = llm
        self.top_k = top_k
        self.max_rewrites = max_rewrites
        self.max_history = max_history
        self.grade_excerpt_length = grade_excerpt_length
        # Texte de personnalité, préparé une seule fois.
        self.persona = PERSONA.format(name=name, role=role)
        self.graph = self.build_graph()

    def build_graph(self):
        """Assemble les étapes de l'agent dans un graphe LangGraph."""
        workflow = StateGraph(AgentState)

        # Chaque nœud est une étape de l'agent. timed() ajoute dans les
        # logs la durée de chaque étape.
        workflow.add_node("analyze_question", timed(self.analyze_question))
        workflow.add_node("prepare_chat", timed(self.prepare_chat))
        workflow.add_node("retrieve", timed(self.retrieve))
        workflow.add_node("grade_documents", timed(self.grade_documents))
        workflow.add_node("transform_query", timed(self.transform_query))
        workflow.add_node("prepare_answer", timed(self.prepare_answer))
        workflow.add_node("fallback", timed(self.fallback))

        # Les arêtes indiquent l'ordre des étapes.
        workflow.add_edge(START, "analyze_question")

        # Aiguillage : question RGPD -> recherche ; sinon -> discussion.
        workflow.add_conditional_edges(
            "analyze_question",
            self.route_question,
            {"retrieve": "retrieve", "prepare_chat": "prepare_chat"},
        )
        workflow.add_edge("retrieve", "grade_documents")

        # Après l'évaluation, decide_next_step choisit l'étape suivante.
        workflow.add_conditional_edges(
            "grade_documents",
            self.decide_next_step,
            {
                "prepare_answer": "prepare_answer",
                "transform_query": "transform_query",
                "fallback": "fallback",
            },
        )

        # Après une reformulation, on relance une recherche (boucle).
        workflow.add_edge("transform_query", "retrieve")
        workflow.add_edge("prepare_answer", END)
        workflow.add_edge("fallback", END)
        workflow.add_edge("prepare_chat", END)

        return workflow.compile()

    # ----- Étapes du graphe -----

    def analyze_question(self, state: AgentState) -> dict:
        """Étape 0 : type de la question et question autonome."""
        history = state.get("history", [])
        if len(history) > 0:
            history_text = format_history(history)
        else:
            history_text = "(aucune)"

        prompt = ANALYZE_PROMPT.format(
            history=history_text,
            question=state["question"],
            language_name=LANGUAGE_NAMES[state["language"]],
        )
        llm_answer = self.llm.invoke(prompt, json_mode=True)
        question_type, standalone_question = parse_analysis(
            llm_answer, state["question"]
        )
        if question_type == "rgpd":
            standalone_question = add_missing_context(
                state["question"], standalone_question, history
            )

        return {
            "question_type": question_type,
            "standalone_question": standalone_question,
        }

    def route_question(self, state: AgentState) -> str:
        """Choisit l'étape suivante après l'analyse de la question."""
        if state["question_type"] == "autre":
            return "prepare_chat"
        return "retrieve"

    def prepare_chat(self, state: AgentState) -> dict:
        """Prompt d'une réponse courte à un message hors RGPD.

        Aucune recherche n'est faite. La réponse elle-même est rédigée
        après le graphe (voir write_answer).
        """
        prompt = CHAT_PROMPT.format(
            persona=self.persona,
            history_block=make_history_block(state.get("history", [])),
            question=state["question"],
            language_name=LANGUAGE_NAMES[state["language"]],
        )
        return {"answer_prompt": prompt, "search_query": ""}

    def retrieve(self, state: AgentState) -> dict:
        """Étape 1 : cherche des extraits du RGPD."""
        # Au premier passage, on cherche avec la question autonome.
        # Après une reformulation, avec la requête reformulée.
        query = state.get("search_query")
        if not query:
            query = state["standalone_question"]

        # Le glossaire ajoute les termes du règlement à la requête
        # (« bases légales » -> « licéité du traitement ») : la recherche
        # trouve mieux les bons articles. Voir src/vocabulary.py.
        expanded_query = expand_query(query, state["language"])
        documents = self.retriever.search(
            expanded_query, self.top_k, state["language"]
        )
        return {"search_query": query, "documents": documents}

    def grade_documents(self, state: AgentState) -> dict:
        """Étape 2 : garde uniquement les extraits jugés pertinents.

        Tous les extraits sont évalués en un seul appel au LLM. Si sa
        réponse est illisible, on les évalue un par un (plus lent, mais
        plus fiable).
        """
        documents = state["documents"]
        if len(documents) == 0:
            return {"relevant_documents": []}

        prompt = GRADE_PROMPT.format(
            question=state["standalone_question"],
            documents=format_numbered_documents(
                documents, self.grade_excerpt_length
            ),
        )
        llm_answer = self.llm.invoke(prompt, json_mode=True)
        numbers = parse_relevant_numbers(llm_answer, len(documents))

        if numbers is None:
            return {"relevant_documents": self.grade_one_by_one(state)}

        # On garde l'ordre de la recherche (du plus proche au plus loin).
        relevant_documents = []
        for number, document in enumerate(documents, start=1):
            if number in numbers:
                relevant_documents.append(document)
        return {"relevant_documents": relevant_documents}

    def grade_one_by_one(self, state: AgentState) -> list[dict]:
        """Évaluation de secours : un appel au LLM par extrait."""
        relevant_documents = []
        for document in state["documents"]:
            prompt = GRADE_ONE_PROMPT.format(
                question=state["standalone_question"],
                document=document["text"],
            )
            llm_answer = self.llm.invoke(prompt, json_mode=True)
            if parse_grade(llm_answer):
                relevant_documents.append(document)
        return relevant_documents

    def decide_next_step(self, state: AgentState) -> str:
        """Choisit l'étape suivante après l'évaluation des extraits."""
        if len(state["relevant_documents"]) > 0:
            return "prepare_answer"

        if state.get("rewrites", 0) < self.max_rewrites:
            return "transform_query"

        return "fallback"

    def transform_query(self, state: AgentState) -> dict:
        """Étape 3a : demande au LLM de reformuler la requête."""
        prompt = REWRITE_PROMPT.format(
            question=state["standalone_question"],
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

    def prepare_answer(self, state: AgentState) -> dict:
        """Étape 3b : prompt de la réponse, avec les extraits pertinents.

        L'historique n'est pas recopié ici : la question autonome
        (réécrite à l'étape 0) contient déjà le contexte utile, et le
        prompt reste plus court, donc plus rapide.
        La réponse elle-même est rédigée après le graphe (voir
        write_answer).
        """
        prompt = GENERATE_PROMPT.format(
            persona=self.persona,
            context=format_context(state["relevant_documents"]),
            question=state["standalone_question"],
            language_name=LANGUAGE_NAMES[state["language"]],
        )
        return {"answer_prompt": prompt}

    def fallback(self, state: AgentState) -> dict:
        """Étape 3c : aucun extrait pertinent, on le dit honnêtement."""
        return {"answer": FALLBACK_ANSWERS[state["language"]]}

    # ----- Utilisation de l'agent -----

    def ask(self, question: str, language: str = "fr", history=None) -> dict:
        """Pose une question à l'agent et retourne sa réponse complète.

        Paramètres :
            question : la nouvelle question
            language : « fr » ou « en »
            history  : échanges précédents de la conversation, sous la
                       forme [{"question": "...", "answer": "..."}, ...]
                       (du plus ancien au plus récent)

        Le dictionnaire retourné contient :
            answer              : la réponse rédigée
            sources             : les articles/considérants utilisés
            standalone_question : la question réécrite avec le contexte
            search_query        : la dernière requête de recherche
                                  (vide pour un message hors RGPD)
            rewrites            : le nombre de reformulations effectuées
        """
        state = self.graph.invoke(
            self.make_initial_state(question, language, history)
        )

        # On rassemble les morceaux de la réponse en un seul texte.
        parts = []
        for part in self.write_answer(state):
            parts.append(part)
        return self.make_result(state, "".join(parts))

    def ask_stream(self, question: str, language: str = "fr", history=None):
        """Comme ask, mais renvoie des événements au fur et à mesure.

        C'est un générateur : chaque tour de boucle donne un événement,
        sous la forme d'un dictionnaire :
            {"type": "status", "step": ...} : étape suivante de l'agent
                « search » (recherche), « grade » (vérification des
                extraits), « rewrite » (reformulation) ou « write »
                (rédaction) : le widget l'affiche pendant l'attente ;
            {"type": "token", "text": ...}  : un morceau de la réponse ;
            {"type": "done", ...}            : fin, avec les mêmes clés
                que le résultat de ask (réponse complète, sources...).
        """
        initial_state = self.make_initial_state(question, language, history)
        # Copie de l'état, complétée après chaque étape du graphe.
        state = dict(initial_state)

        # stream_mode="updates" : après chaque étape, LangGraph donne le
        # nom de l'étape et les clés qu'elle a modifiées. On les recopie
        # dans « state » pour garder l'état complet.
        updates = self.graph.stream(initial_state, stream_mode="updates")
        for update in updates:
            for step_name, changes in update.items():
                state.update(changes)
                next_step = self.describe_next_step(step_name, state)
                if next_step is not None:
                    yield {"type": "status", "step": next_step}

        parts = []
        for part in self.write_answer(state):
            parts.append(part)
            yield {"type": "token", "text": part}

        result = self.make_result(state, "".join(parts))
        result["type"] = "done"
        yield result

    def make_initial_state(self, question, language, history) -> dict:
        """État de départ du graphe pour une nouvelle question."""
        if history is None:
            history = []

        # On ne garde que les échanges les plus récents : au-delà, le
        # prompt devient long et le LLM plus lent.
        if len(history) > self.max_history:
            history = history[-self.max_history :]

        return {
            "question": question,
            "language": language,
            "history": history,
            "rewrites": 0,
        }

    def write_answer(self, state: dict):
        """Étape 4 : rédige la réponse, morceau par morceau.

        Si le graphe a déjà fixé la réponse (cas « fallback »), on la
        renvoie en un seul morceau, sans appeler le LLM.
        """
        if not state.get("answer_prompt"):
            yield state["answer"]
            return

        start = time.perf_counter()
        # « yield from » renvoie un par un les morceaux du LLM.
        yield from self.llm.stream(state["answer_prompt"])
        duration = time.perf_counter() - start
        logger.info("Étape write_answer : %.1f s", duration)

    def describe_next_step(self, step_name: str, state: dict):
        """Étape suivante à annoncer à l'utilisateur, ou None.

        Le graphe ne dit pas quelle étape va suivre : on la déduit de
        l'étape qui vient de se terminer et de l'état.
        """
        if step_name == "analyze_question":
            if state["question_type"] == "autre":
                return "write"
            return "search"

        if step_name == "retrieve":
            return "grade"

        if step_name == "grade_documents":
            if len(state["relevant_documents"]) > 0:
                return "write"
            if state.get("rewrites", 0) < self.max_rewrites:
                return "rewrite"
            # Plus d'essais : la réponse « je ne sais pas » est immédiate.
            return None

        if step_name == "transform_query":
            return "search"

        return None

    def make_result(self, state: dict, answer: str) -> dict:
        """Résultat renvoyé à l'API à partir de l'état final."""
        # Liste des sources, sans doublon et dans l'ordre d'apparition.
        sources = []
        for document in state.get("relevant_documents", []):
            if document["section"] not in sources:
                sources.append(document["section"])

        return {
            "answer": answer.strip(),
            "sources": sources,
            "standalone_question": state["standalone_question"],
            "search_query": state.get("search_query", ""),
            "rewrites": state.get("rewrites", 0),
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
    return CragAgent(
        retriever,
        llm,
        settings.top_k,
        settings.max_rewrites,
        settings.max_history,
        settings.assistant_name,
        settings.assistant_role,
        settings.grade_excerpt_length,
    )
