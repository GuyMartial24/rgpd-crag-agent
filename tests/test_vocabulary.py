"""Tests du glossaire (src/vocabulary.py)."""

import pytest

from src.vocabulary import expand_query


@pytest.mark.parametrize(
    ("query", "language", "expected"),
    [
        # Terme courant -> terme du règlement ajouté entre parenthèses.
        (
            "Quelles sont les bases légales ?",
            "fr",
            "Quelles sont les bases légales ? (licéité du traitement)",
        ),
        (
            "Ai-je un droit à l'oubli ?",
            "fr",
            "Ai-je un droit à l'oubli ? (droit à l'effacement)",
        ),
        # Majuscules ignorées.
        (
            "Faut-il un DPO ?",
            "fr",
            "Faut-il un DPO ? (délégué à la protection des données)",
        ),
        # Plusieurs termes dans la même question.
        (
            "Quelles sanctions sans base légale ?",
            "fr",
            "Quelles sanctions sans base légale ? "
            "(licéité du traitement, amendes administratives)",
        ),
        (
            "What is the legal basis?",
            "en",
            "What is the legal basis? (lawfulness of processing)",
        ),
        # Aucun terme du glossaire : la requête est inchangée.
        (
            "Qu'est-ce qu'une donnée sensible ?",
            "fr",
            "Qu'est-ce qu'une donnée sensible ?",
        ),
        # Mot entier seulement : « amendement » n'est pas « amende ».
        (
            "Un amendement est-il prévu ?",
            "fr",
            "Un amendement est-il prévu ?",
        ),
        # Terme du règlement déjà présent : pas de doublon.
        (
            "La licéité du traitement exige-t-elle une base légale ?",
            "fr",
            "La licéité du traitement exige-t-elle une base légale ?",
        ),
        # Les termes français ne s'appliquent pas à une question en anglais.
        ("What about the CNIL?", "en", "What about the CNIL?"),
    ],
)
def test_expand_query(query, language, expected):
    assert expand_query(query, language) == expected
