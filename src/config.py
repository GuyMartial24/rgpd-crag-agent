"""Configuration de l'application.

Toutes les valeurs de la classe Settings peuvent être modifiées sans
toucher au code, avec une variable d'environnement ou un fichier .env.
Exemple : LLM_MODEL=mistral remplace la valeur de « llm_model ».
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Dossier racine du projet : le dossier parent de « src ».
ROOT_DIR = Path(__file__).resolve().parent.parent

# Dossier contenant les PDF officiels du RGPD.
DATA_DIR = ROOT_DIR / "data"


class Settings(BaseSettings):
    """Paramètres de l'application (lus dans l'environnement)."""

    # On lit le fichier .env s'il existe. Les variables inconnues sont
    # ignorées au lieu de provoquer une erreur.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ----- Ollama : serveur qui fait tourner les modèles en local -----
    ollama_base_url: str = "http://localhost:11434"
    # Modèle de langage qui évalue les extraits et rédige les réponses.
    llm_model: str = "llama3.2:3b"
    # Modèle qui transforme un texte en vecteur (embedding).
    # bge-m3 est multilingue : il fonctionne bien en français.
    embedding_model: str = "bge-m3"
    # Température à 0 : réponses les plus stables possibles.
    llm_temperature: float = 0.0
    # Temps maximum (en secondes) accordé à un appel à Ollama.
    request_timeout: float = 120.0

    # ----- Corpus et base vectorielle -----
    # Textes officiels du RGPD (Journal officiel L 119 du 4 mai 2016),
    # un fichier PDF par langue.
    corpus_files: dict[str, Path] = {
        "fr": DATA_DIR / "RGPD_FR.pdf",
        "en": DATA_DIR / "RGPD_EN.pdf",
    }
    # Dossier où ChromaDB enregistre la base vectorielle.
    chroma_path: Path = ROOT_DIR / "chroma_db"
    # Nom de la collection (l'équivalent d'une table) dans ChromaDB.
    collection_name: str = "rgpd"
    # Taille maximale d'un morceau de texte (en caractères).
    chunk_size: int = 1000
    # Nombre de caractères répétés entre deux morceaux qui se suivent,
    # pour ne pas couper une idée en deux.
    chunk_overlap: int = 150
    # Nombre de morceaux envoyés en même temps au modèle d'embedding.
    embed_batch_size: int = 32

    # ----- Agent CRAG -----
    # Nombre d'extraits récupérés à chaque recherche.
    top_k: int = 4
    # Nombre maximum de reformulations de la question avant d'abandonner.
    max_rewrites: int = 2
    # Nombre maximum d'échanges passés (question + réponse) pris en
    # compte pour la mémoire de conversation.
    max_history: int = 6

    # ----- Personnalité de l'assistant -----
    # Nom et titre utilisés dans le prompt de génération des réponses.
    assistant_name: str = "Martial"
    assistant_role: str = "Data & AI Engineer"

    # ----- API -----
    # Sites autorisés à appeler l'API depuis le navigateur (CORS), par
    # exemple quand le widget de chat est intégré sur un autre site.
    # « * » autorise tous les sites. En production, on indique plutôt
    # la liste précise, ex. : CORS_ORIGINS='["https://mon-site.fr"]'
    cors_origins: list[str] = ["*"]


# On garde une seule instance des paramètres pour toute l'application.
_settings = None


def get_settings() -> Settings:
    """Retourne les paramètres (créés une seule fois, au premier appel)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
