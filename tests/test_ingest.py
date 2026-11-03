"""Tests de l'ingestion : lecture des PDF, découpage et ChromaDB.

Ces tests n'ont pas besoin d'Ollama : les embeddings sont calculés par
un faux modèle (FakeEmbedder), simple et rapide.
"""

import re
import zlib

import chromadb
import pytest

from src.config import Settings
from src.ingest import (
    ChromaRetriever,
    build_chunks,
    check_embedding_model,
    chunk_sections,
    export_index,
    extract_pdf_text,
    find_missing_chunks,
    get_collection,
    index_chunks,
    ingest,
    parse_regulation,
    restore_prebuilt_index,
)

# Petit extrait au format du Journal officiel, tel que le renvoie
# extract_pdf_text (pieds de page déjà supprimés).
SAMPLE_FR = """RÈGLEMENT (UE) 2016/679 DU PARLEMENT EUROPÉEN ET DU CONSEIL
considérant ce qui suit:
(1) La protection des personnes physiques est un droit fondamental (1).
(2) Tout traitement de données à caractère personnel devrait être licite.
ONT ADOPTÉ LE PRÉSENT RÈGLEMENT:
CHAPITRE I
Dispositions générales
Article premier
Objet et objectifs
1. Le présent règlement établit des règles relatives aux données.
CHAPITRE IV
Responsable du traitement et sous-traitant
Section 2
Sécurité des données à caractère personnel
Article 33
Notification à l'autorité de contrôle d'une violation de données
1. La violation est notifiée 72 heures au plus tard.
Le présent règlement est obligatoire dans tous ses éléments.
Fait à Bruxelles, le 27 avril 2016."""

SAMPLE_EN = """REGULATION (EU) 2016/679
Whereas:
(1) The protection of natural persons is a fundamental right.
HAVE ADOPTED THIS REGULATION:
CHAPTER I
General provisions
Article 1
Subject-matter and objectives
1. This Regulation lays down rules relating to personal data.
This Regulation shall be binding in its entirety."""


class FakeEmbedder:
    """Faux modèle d'embedding, déterministe et sans Ollama.

    Chaque mot est associé à une case d'un vecteur de 256 nombres
    (grâce à une fonction de hachage). Deux textes qui partagent des
    mots auront donc des vecteurs proches.
    """

    size = 256

    def embed(self, texts):
        vectors = []
        for text in texts:
            vector = [0.0] * self.size
            for word in re.findall(r"\w+", text.lower()):
                position = zlib.crc32(word.encode()) % self.size
                vector[position] += 1.0
            vectors.append(vector)
        return vectors


# ----- Fixtures : objets préparés automatiquement pour les tests -----


@pytest.fixture
def collection(tmp_path):
    """Collection ChromaDB vide, créée dans un dossier temporaire."""
    settings = Settings(
        chroma_path=tmp_path / "chroma", collection_name="test-rgpd"
    )
    client = chromadb.PersistentClient(path=str(settings.chroma_path))
    return get_collection(settings, client)


@pytest.fixture
def indexed_collection(collection):
    """Collection contenant les extraits français et anglais ci-dessus."""
    sections_fr = parse_regulation(SAMPLE_FR, "fr")
    sections_en = parse_regulation(SAMPLE_EN, "en")
    chunks = chunk_sections(sections_fr, "fr", 1000, 0)
    chunks = chunks + chunk_sections(sections_en, "en", 1000, 0)

    index_chunks(chunks, collection, FakeEmbedder(), batch_size=2)
    return collection


def get_names(sections):
    """Retourne la liste des noms de sections."""
    names = []
    for section in sections:
        names.append(section["name"])
    return names


# ----- Découpage en considérants et en articles -----


def test_parse_regulation_splits_recitals_and_articles():
    sections = parse_regulation(SAMPLE_FR, "fr")

    assert get_names(sections) == [
        "Considérant 1",
        "Considérant 2",
        "Article 1",
        "Article 33",
    ]
    # Le nom du considérant est ajouté en tête de son texte.
    assert sections[1]["text"].startswith("Considérant 2\nTout traitement")
    # « Article premier » est bien reconnu comme l'article 1.
    assert sections[2]["text"].startswith("Article premier\nObjet")


def test_parse_regulation_removes_headings_and_signatures():
    sections = parse_regulation(SAMPLE_FR, "fr")
    all_text = ""
    for section in sections:
        all_text = all_text + section["text"] + "\n"

    assert "CHAPITRE" not in all_text
    assert "Section 2" not in all_text
    assert "Responsable du traitement et sous-traitant" not in all_text
    assert "Fait à Bruxelles" not in all_text


def test_parse_regulation_english():
    sections = parse_regulation(SAMPLE_EN, "en")

    assert get_names(sections) == ["Recital 1", "Article 1"]
    assert "binding" not in sections[1]["text"]


def test_parse_regulation_rejects_unknown_text():
    with pytest.raises(ValueError):
        parse_regulation("Texte sans structure de règlement", "fr")


def test_recitals_ignore_out_of_sequence_numbers():
    # Un « (5) » en début de ligne ne doit pas être pris pour un
    # considérant, car on attend le numéro 2.
    text = SAMPLE_FR.replace(
        "(2) Tout traitement",
        "(5) renvoi en début de ligne\n(2) Tout traitement",
    )
    names = get_names(parse_regulation(text, "fr"))

    assert names[0] == "Considérant 1"
    assert names[1] == "Considérant 2"


# ----- Découpage en morceaux (chunks) -----


def test_chunk_sections_respects_size_and_keeps_context():
    long_text = "Article 17\nDroit à l'effacement\n" + "phrase de test. " * 200
    sections = [{"name": "Article 17", "text": long_text}]

    chunks = chunk_sections(sections, "fr", chunk_size=300, chunk_overlap=50)

    assert len(chunks) > 1
    for number, chunk in enumerate(chunks):
        expected_metadata = {
            "section": "Article 17",
            "lang": "fr",
            "chunk": number,
        }
        assert chunk["metadata"] == expected_metadata
        # 300 caractères maximum, plus le préfixe « [Article 17] ».
        assert len(chunk["text"]) <= 300 + len("[Article 17] ")

    # À partir du 2e morceau, le nom de l'article est rappelé.
    for chunk in chunks[1:]:
        assert chunk["text"].startswith("[Article 17] ")


def test_chunk_ids_are_unique_across_languages():
    chunks = chunk_sections(parse_regulation(SAMPLE_FR, "fr"), "fr", 1000, 0)
    chunks += chunk_sections(parse_regulation(SAMPLE_EN, "en"), "en", 1000, 0)

    ids = []
    for chunk in chunks:
        ids.append(chunk["id"])

    assert "fr-considerant-1-0" in ids
    assert "en-article-1-0" in ids
    # Un set supprime les doublons : même taille = aucun doublon.
    assert len(set(ids)) == len(ids)


# ----- Lecture des vrais PDF -----


def test_extract_pdf_text_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_pdf_text(tmp_path / "absent.pdf")


@pytest.mark.parametrize("language", ["fr", "en"])
def test_official_pdfs_are_fully_parsed(language):
    pdf_path = Settings().corpus_files[language]
    text = extract_pdf_text(pdf_path)
    names = get_names(parse_regulation(text, language))

    if language == "fr":
        recital_name = "Considérant"
    else:
        recital_name = "Recital"

    # Le RGPD contient 173 considérants puis 99 articles.
    expected_names = []
    for number in range(1, 174):
        expected_names.append(f"{recital_name} {number}")
    for number in range(1, 100):
        expected_names.append(f"Article {number}")

    assert names == expected_names
    # Plus aucun pied de page du Journal officiel dans le texte.
    assert re.search(r"^\d+\.\d+\.2016 L 119/", text, re.MULTILINE) is None


# ----- Recherche dans ChromaDB -----


def test_search_filters_by_language(indexed_collection):
    retriever = ChromaRetriever(indexed_collection, FakeEmbedder())

    fr_results = retriever.search(
        "notification violation 72 heures", k=2, language="fr"
    )
    en_results = retriever.search(
        "protection personal data", k=5, language="en"
    )

    # Le meilleur résultat français est l'article 33.
    assert fr_results[0]["section"] == "Article 33"
    # Les résultats sont triés du plus proche au plus éloigné.
    assert fr_results[0]["distance"] <= fr_results[1]["distance"]
    # Avec le filtre « en », seuls les extraits anglais sont renvoyés.
    en_sections = set()
    for document in en_results:
        en_sections.add(document["section"])
    assert en_sections == {"Recital 1", "Article 1"}


def test_reindexing_does_not_create_duplicates(indexed_collection):
    # 4 sections françaises + 2 sections anglaises = 6 morceaux.
    assert indexed_collection.count() == 6

    chunks = chunk_sections(parse_regulation(SAMPLE_FR, "fr"), "fr", 1000, 0)
    index_chunks(chunks, indexed_collection, FakeEmbedder())

    assert indexed_collection.count() == 6


# ----- Ingestion reprenable -----


def test_find_missing_chunks_resumes_interrupted_ingestion(collection):
    chunks = chunk_sections(parse_regulation(SAMPLE_FR, "fr"), "fr", 1000, 0)

    # Simulation d'une ingestion interrompue : seuls les 2 premiers
    # morceaux (sur 4) ont été enregistrés.
    index_chunks(chunks[:2], collection, FakeEmbedder())

    missing = find_missing_chunks(chunks, collection)

    # Seuls les 2 morceaux restants sont à calculer.
    assert missing == chunks[2:]


def test_find_missing_chunks_when_base_is_complete(indexed_collection):
    chunks = chunk_sections(parse_regulation(SAMPLE_FR, "fr"), "fr", 1000, 0)

    assert find_missing_chunks(chunks, indexed_collection) == []


def test_find_missing_chunks_on_empty_base(collection):
    chunks = chunk_sections(parse_regulation(SAMPLE_FR, "fr"), "fr", 1000, 0)

    assert find_missing_chunks(chunks, collection) == chunks


# ----- Vérification du modèle d'embedding -----


def test_collection_remembers_embedding_model(collection):
    assert collection.metadata["embedding_model"] == "bge-m3"


def test_same_embedding_model_is_accepted(collection):
    # Aucune erreur : la base a été créée avec le modèle configuré.
    check_embedding_model(collection, Settings(embedding_model="bge-m3"))


def test_different_embedding_model_is_refused(collection):
    with pytest.raises(ValueError, match="--reset"):
        check_embedding_model(
            collection, Settings(embedding_model="nomic-embed-text")
        )


def test_old_base_without_model_is_accepted(tmp_path):
    # Base créée par une ancienne version, sans le nom du modèle.
    client = chromadb.PersistentClient(path=str(tmp_path / "ancienne"))
    old_collection = client.create_collection(
        name="rgpd", metadata={"hnsw:space": "cosine"}
    )

    check_embedding_model(old_collection, Settings())


# ----- Base fournie avec le projet (data/chroma_db.tar.gz) -----


def test_provided_index_is_complete_and_up_to_date(tmp_path):
    # La base fournie doit contenir exactement les morceaux produits par
    # le découpage actuel : sinon, il faut la régénérer (voir README).
    settings = Settings(chroma_path=tmp_path / "chroma")

    assert restore_prebuilt_index(settings)

    collection = get_collection(settings)
    check_embedding_model(collection, settings)
    assert find_missing_chunks(build_chunks(settings), collection) == []


def test_ingest_uses_provided_index_without_ollama(tmp_path):
    # Base vide : l'archive est décompressée et rien n'est à calculer.
    # Aucun appel à Ollama n'est fait (il n'est pas lancé pendant les
    # tests) : sinon, ce test échouerait.
    settings = Settings(chroma_path=tmp_path / "chroma")

    assert ingest(settings) == 0


def test_existing_base_is_never_overwritten(tmp_path):
    settings = Settings(chroma_path=tmp_path / "chroma")
    settings.chroma_path.mkdir()
    (settings.chroma_path / "fichier-existant").write_text("ma base")

    assert not restore_prebuilt_index(settings)
    assert (settings.chroma_path / "fichier-existant").exists()


def test_provided_index_ignored_with_another_model(tmp_path):
    settings = Settings(
        chroma_path=tmp_path / "chroma", embedding_model="nomic-embed-text"
    )

    assert not restore_prebuilt_index(settings)
    assert not settings.chroma_path.exists()


def test_missing_archive_is_ignored(tmp_path):
    settings = Settings(
        chroma_path=tmp_path / "chroma",
        prebuilt_index_path=tmp_path / "absente.tar.gz",
    )

    assert not restore_prebuilt_index(settings)


def test_export_then_restore_gives_the_same_base(indexed_collection, tmp_path):
    # indexed_collection est enregistrée dans tmp_path / "chroma".
    source = Settings(
        chroma_path=tmp_path / "chroma",
        collection_name="test-rgpd",
        prebuilt_index_path=tmp_path / "export.tar.gz",
    )
    count = indexed_collection.count()

    export_index(source)
    copy = Settings(
        chroma_path=tmp_path / "copie",
        collection_name="test-rgpd",
        prebuilt_index_path=source.prebuilt_index_path,
    )

    assert restore_prebuilt_index(copy)
    assert get_collection(copy).count() == count
