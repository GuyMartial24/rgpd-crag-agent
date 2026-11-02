"""Ingestion du RGPD dans la base vectorielle ChromaDB.

Étapes de l'ingestion :
    1. lire le texte des PDF officiels (français et anglais) ;
    2. découper ce texte en considérants et en articles ;
    3. couper les sections trop longues en morceaux (« chunks ») ;
    4. calculer un vecteur (embedding) pour chaque morceau avec Ollama ;
    5. enregistrer les morceaux et leurs vecteurs dans ChromaDB.

Utilisation en ligne de commande :
    python -m src.ingest           # indexe uniquement les morceaux manquants
    python -m src.ingest --reset   # vide la base puis réindexe tout

L'ingestion est « reprenable » : seuls les morceaux absents de la base
sont calculés. Si elle est interrompue, il suffit de la relancer pour
qu'elle reprenne là où elle s'était arrêtée. Si la base est déjà
complète, elle se termine en quelques secondes.
"""

import argparse
import logging
import re
import unicodedata
from pathlib import Path

import chromadb
from langchain_text_splitters import RecursiveCharacterTextSplitter
from ollama import Client
from pypdf import PdfReader

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Phrases propres à chaque langue, qui servent de repères pour trouver
# les différentes parties du règlement dans le texte.
MARKERS = {
    "fr": {
        # Nom donné aux considérants dans les résultats.
        "recital_name": "Considérant",
        # Phrase qui annonce le début des considérants.
        "recitals_start": "considérant ce qui suit",
        # Phrase qui annonce le début des articles.
        "articles_start": "ONT ADOPTÉ LE PRÉSENT RÈGLEMENT",
        # Phrase finale, suivie des signatures (que l'on ne garde pas).
        "end": "Le présent règlement est obligatoire dans tous ses éléments",
    },
    "en": {
        "recital_name": "Recital",
        "recitals_start": "Whereas:",
        "articles_start": "HAVE ADOPTED THIS REGULATION",
        "end": "This Regulation shall be binding in its entirety",
    },
}

# Expressions régulières utilisées pour analyser le texte.
# re.MULTILINE permet à « ^ » et « $ » de désigner le début et la fin
# de chaque ligne (et pas seulement du texte entier).

# Pied de page du Journal officiel, par exemple :
# « 4.5.2016 L 119/2 Journal officiel de l'Union européenne FR »
FOOTER_PATTERN = re.compile(
    r"^\d{1,2}\.\d{1,2}\.\d{4} L \d+/\d+ .*$", re.MULTILINE
)
# Titre de chapitre : « CHAPITRE IV » puis le titre sur la ligne suivante.
CHAPTER_PATTERN = re.compile(
    r"^(CHAPITRE|CHAPTER) [IVXL]+\n.*\n", re.MULTILINE
)
# Titre de section : « Section 2 » puis le titre sur la ligne suivante.
SECTION_PATTERN = re.compile(r"^Section \d+\n.*\n", re.MULTILINE)
# En-tête d'article : « Article 5 » ou « Article premier » (en français).
ARTICLE_PATTERN = re.compile(r"^Article (premier|\d+)$", re.MULTILINE)
# Début d'un considérant : un numéro entre parenthèses, ex. « (39) ».
RECITAL_PATTERN = re.compile(r"^\((\d+)\)\s+", re.MULTILINE)


# ---------------------------------------------------------------------
# 1. Lecture des PDF
# ---------------------------------------------------------------------


def extract_pdf_text(pdf_path: Path) -> str:
    """Retourne le texte d'un PDF du Journal officiel.

    Les pieds de page et les notes de bas de page sont supprimés.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"Corpus introuvable : {pdf_path}")

    reader = PdfReader(pdf_path)
    pages_text = []

    for page in reader.pages:
        text = page.extract_text()
        if text is None:
            text = ""

        # Sur chaque page, le pied de page est suivi des notes de bas de
        # page : on garde uniquement le texte situé avant le pied de page.
        footer = FOOTER_PATTERN.search(text)
        if footer is not None:
            text = text[: footer.start()]

        pages_text.append(text)

    full_text = "\n".join(pages_text)
    return clean_text(full_text)


def clean_text(text: str) -> str:
    """Supprime les espaces en début/fin de ligne et les lignes vides."""
    clean_lines = []
    for line in text.splitlines():
        line = line.strip()
        if line != "":
            clean_lines.append(line)
    return "\n".join(clean_lines)


# ---------------------------------------------------------------------
# 2. Découpage en considérants et en articles
# ---------------------------------------------------------------------


def parse_regulation(text: str, language: str) -> list[dict]:
    """Découpe le texte du règlement en sections.

    Chaque section est un dictionnaire :
        {"name": "Article 5", "text": "Article 5\\nPrincipes..."}

    Les considérants viennent d'abord, puis les articles.
    """
    markers = MARKERS[language]

    # On cherche la position des deux grandes parties du règlement.
    recitals_start = text.find(markers["recitals_start"])
    articles_start = text.find(markers["articles_start"])
    if recitals_start == -1 or articles_start == -1:
        raise ValueError(
            f"Structure du règlement non reconnue pour la langue "
            f"« {language} »"
        )

    # Partie 1 : les considérants, entre leur phrase d'introduction et
    # le début des articles.
    recitals_text = text[recitals_start:articles_start]

    # Partie 2 : les articles, jusqu'à la phrase finale (on retire ainsi
    # les signatures en fin de document).
    articles_text = text[articles_start:]
    end_position = articles_text.find(markers["end"])
    if end_position != -1:
        articles_text = articles_text[:end_position]

    # Les titres de chapitre et de section se trouvent entre deux
    # articles : on les retire pour qu'ils ne se collent pas à la fin
    # de l'article précédent. Le "\n" ajouté permet de reconnaître un
    # titre placé tout à la fin du texte.
    articles_text = articles_text + "\n"
    articles_text = CHAPTER_PATTERN.sub("", articles_text)
    articles_text = SECTION_PATTERN.sub("", articles_text)

    recitals = split_recitals(recitals_text, markers["recital_name"])
    articles = split_articles(articles_text)
    return recitals + articles


def split_recitals(text: str, recital_name: str) -> list[dict]:
    """Découpe la partie « considérants » en une section par considérant."""
    # Les considérants commencent par (1), (2), (3)... en début de ligne.
    # Attention : les renvois aux notes de bas de page utilisent aussi ce
    # format. On ne garde donc que le numéro attendu (1, puis 2, puis 3,
    # etc.) pour ne pas se tromper.
    recital_matches = []
    expected_number = 1
    for match in RECITAL_PATTERN.finditer(text):
        number = int(match.group(1))
        if number == expected_number:
            recital_matches.append(match)
            expected_number = expected_number + 1

    sections = []
    for index, match in enumerate(recital_matches):
        # Un considérant s'arrête là où commence le suivant
        # (ou à la fin du texte pour le dernier).
        is_last = index == len(recital_matches) - 1
        if is_last:
            end = len(text)
        else:
            end = recital_matches[index + 1].start()

        name = f"{recital_name} {match.group(1)}"
        content = text[match.end() : end].strip()
        # On remet le nom en tête du texte pour donner du contexte au LLM.
        sections.append({"name": name, "text": name + "\n" + content})

    return sections


def split_articles(text: str) -> list[dict]:
    """Découpe la partie « articles » en une section par article."""
    article_matches = list(ARTICLE_PATTERN.finditer(text))

    sections = []
    for index, match in enumerate(article_matches):
        is_last = index == len(article_matches) - 1
        if is_last:
            end = len(text)
        else:
            end = article_matches[index + 1].start()

        # En français, l'article 1 s'appelle « Article premier ».
        number = match.group(1)
        if number == "premier":
            number = "1"

        name = f"Article {number}"
        content = text[match.start() : end].strip()
        sections.append({"name": name, "text": content})

    return sections


# ---------------------------------------------------------------------
# 3. Découpage en morceaux (chunks)
# ---------------------------------------------------------------------


def make_chunk_id(language: str, section_name: str, chunk_number: int) -> str:
    """Construit un identifiant unique, par exemple « fr-article-5-0 »."""
    # Suppression des accents : « Considérant » devient « Considerant ».
    name = unicodedata.normalize("NFKD", section_name)
    name = name.encode("ascii", "ignore").decode()
    # Minuscules, et tout ce qui n'est ni lettre ni chiffre devient « - ».
    name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{language}-{name}-{chunk_number}"


def chunk_sections(
    sections: list[dict],
    language: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[dict]:
    """Coupe chaque section en morceaux de taille limitée.

    Chaque morceau est un dictionnaire avec trois clés :
    « id », « text » et « metadata ».
    """
    # Le découpeur essaie d'abord de couper entre deux paragraphes, puis
    # entre deux lignes, puis entre deux phrases, et en dernier recours
    # au milieu d'un mot.
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    chunks = []
    for section in sections:
        pieces = splitter.split_text(section["text"])

        for chunk_number, piece in enumerate(pieces):
            # Seul le premier morceau contient l'en-tête « Article X ».
            # On l'ajoute aux morceaux suivants pour garder le contexte.
            if chunk_number > 0:
                piece = f"[{section['name']}] {piece}"

            chunk = {
                "id": make_chunk_id(language, section["name"], chunk_number),
                "text": piece,
                "metadata": {
                    "section": section["name"],
                    "lang": language,
                    "chunk": chunk_number,
                },
            }
            chunks.append(chunk)

    return chunks


def build_chunks(settings: Settings) -> list[dict]:
    """Lit tous les PDF du corpus et retourne la liste de leurs morceaux."""
    all_chunks = []

    for language, pdf_path in settings.corpus_files.items():
        text = extract_pdf_text(pdf_path)
        sections = parse_regulation(text, language)
        logger.info(
            "%s : %d sections extraites de %s",
            language,
            len(sections),
            pdf_path.name,
        )

        chunks = chunk_sections(
            sections,
            language,
            settings.chunk_size,
            settings.chunk_overlap,
        )
        all_chunks.extend(chunks)

    return all_chunks


# ---------------------------------------------------------------------
# 4. Calcul des embeddings avec Ollama
# ---------------------------------------------------------------------


class OllamaEmbedder:
    """Transforme des textes en vecteurs avec un modèle Ollama."""

    def __init__(self, settings: Settings):
        self.client = Client(
            host=settings.ollama_base_url,
            timeout=settings.request_timeout,
        )
        self.model = settings.embedding_model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Retourne un vecteur par texte, dans le même ordre."""
        response = self.client.embed(model=self.model, input=texts)

        vectors = []
        for vector in response.embeddings:
            vectors.append(list(vector))
        return vectors


# ---------------------------------------------------------------------
# 5. Stockage et recherche dans ChromaDB
# ---------------------------------------------------------------------


def get_chroma_client(settings: Settings):
    """Ouvre la base ChromaDB enregistrée sur le disque."""
    return chromadb.PersistentClient(path=str(settings.chroma_path))


def get_collection(settings: Settings, client=None):
    """Retourne la collection ChromaDB (elle est créée si besoin)."""
    if client is None:
        client = get_chroma_client(settings)

    # embedding_function=None : les vecteurs sont calculés par Ollama et
    # non par ChromaDB. « cosine » : similarité cosinus entre vecteurs.
    # On note aussi le modèle d'embedding utilisé : les vecteurs de deux
    # modèles différents ne sont pas comparables entre eux. Ces
    # informations ne sont enregistrées qu'à la création de la collection.
    return client.get_or_create_collection(
        name=settings.collection_name,
        embedding_function=None,
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": settings.embedding_model,
        },
    )


def check_embedding_model(collection, settings: Settings):
    """Vérifie que la base a été construite avec le modèle configuré.

    Mélanger des vecteurs de deux modèles différents rendrait la
    recherche incohérente (ou provoquerait une erreur de dimension) :
    on s'arrête avec un message clair plutôt que de continuer.
    """
    metadata = collection.metadata
    if metadata is None:
        metadata = {}

    stored_model = metadata.get("embedding_model")

    # Base créée par une ancienne version du projet : le modèle n'avait
    # pas été enregistré, on ne peut pas vérifier.
    if stored_model is None:
        logger.warning(
            "Modèle d'embedding de la base inconnu (ancienne version) : "
            "vérification impossible."
        )
        return

    if stored_model != settings.embedding_model:
        raise ValueError(
            f"La base a été construite avec le modèle « {stored_model} » "
            f"mais EMBEDDING_MODEL vaut « {settings.embedding_model} ». "
            f"Reconstruisez la base avec : python -m src.ingest --reset"
        )


def find_missing_chunks(chunks: list[dict], collection) -> list[dict]:
    """Retourne les morceaux qui ne sont pas encore dans la collection."""
    all_ids = []
    for chunk in chunks:
        all_ids.append(chunk["id"])

    # On demande à ChromaDB lesquels de ces identifiants il connaît déjà
    # (include=[] : on ne récupère que les identifiants, pas les textes).
    existing = collection.get(ids=all_ids, include=[])
    existing_ids = set(existing["ids"])

    missing_chunks = []
    for chunk in chunks:
        if chunk["id"] not in existing_ids:
            missing_chunks.append(chunk)
    return missing_chunks


def index_chunks(chunks, collection, embedder, batch_size=32) -> int:
    """Enregistre les morceaux et leurs vecteurs dans la collection.

    Les morceaux sont envoyés par lots de « batch_size » pour ne pas
    surcharger Ollama. Retourne le nombre de morceaux indexés.
    """
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]

        ids = []
        texts = []
        metadatas = []
        for chunk in batch:
            ids.append(chunk["id"])
            texts.append(chunk["text"])
            metadatas.append(chunk["metadata"])

        # upsert = « insérer ou mettre à jour » : relancer l'ingestion
        # ne crée pas de doublons, les morceaux existants sont remplacés.
        collection.upsert(
            ids=ids,
            documents=texts,
            embeddings=embedder.embed(texts),
            metadatas=metadatas,
        )

        done = min(start + batch_size, len(chunks))
        logger.info("Indexé %d/%d chunks", done, len(chunks))

    return len(chunks)


class ChromaRetriever:
    """Recherche les morceaux les plus proches d'une question."""

    def __init__(self, collection, embedder):
        self.collection = collection
        self.embedder = embedder

    def search(self, query: str, k: int, language=None) -> list[dict]:
        """Retourne les k morceaux les plus proches de la requête.

        Si « language » est précisé (« fr » ou « en »), seuls les
        morceaux de cette langue sont cherchés.
        """
        if language is None:
            where_filter = None
        else:
            where_filter = {"lang": language}

        query_vector = self.embedder.embed([query])
        result = self.collection.query(
            query_embeddings=query_vector,
            n_results=k,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )

        # ChromaDB renvoie une liste de résultats par requête. Nous
        # n'envoyons qu'une seule requête, d'où l'index [0].
        texts = result["documents"][0]
        metadatas = result["metadatas"][0]
        distances = result["distances"][0]

        documents = []
        for i in range(len(texts)):
            documents.append(
                {
                    "text": texts[i],
                    "section": metadatas[i]["section"],
                    "distance": distances[i],
                }
            )
        return documents


# ---------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------


def ingest(settings=None, reset=False) -> int:
    """Lance l'ingestion. Retourne le nombre de morceaux indexés.

    Seuls les morceaux absents de la base sont calculés : une ingestion
    interrompue reprend là où elle s'était arrêtée.
    """
    if settings is None:
        settings = get_settings()

    client = get_chroma_client(settings)

    # Option --reset : on supprime la collection pour repartir de zéro.
    if reset:
        existing_names = []
        for collection in client.list_collections():
            existing_names.append(collection.name)

        if settings.collection_name in existing_names:
            client.delete_collection(settings.collection_name)
            logger.info(
                "Collection « %s » supprimée", settings.collection_name
            )

    collection = get_collection(settings, client)
    check_embedding_model(collection, settings)

    chunks = build_chunks(settings)
    missing_chunks = find_missing_chunks(chunks, collection)

    # Base déjà complète (cas d'un simple redémarrage) : rien à calculer.
    if len(missing_chunks) == 0:
        logger.info(
            "Base déjà complète (%d chunks) : rien à indexer", len(chunks)
        )
        return 0

    logger.info(
        "%d chunks à indexer sur %d (les autres sont déjà dans la base)",
        len(missing_chunks),
        len(chunks),
    )
    embedder = OllamaEmbedder(settings)
    return index_chunks(
        missing_chunks, collection, embedder, settings.embed_batch_size
    )


def main():
    """Point d'entrée de la commande « python -m src.ingest »."""
    parser = argparse.ArgumentParser(
        description="Indexe le RGPD (FR/EN) dans ChromaDB."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="supprime puis reconstruit la collection",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    count = ingest(reset=args.reset)
    logger.info("Ingestion terminée : %d chunks indexés", count)


# Ce bloc ne s'exécute que si le fichier est lancé directement.
if __name__ == "__main__":
    main()
