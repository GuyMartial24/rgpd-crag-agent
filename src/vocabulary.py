"""Glossaire : du vocabulaire courant à celui du RGPD.

Les utilisateurs emploient des mots courants (« bases légales »,
« droit à l'oubli », « fuite de données »), alors que le règlement
utilise ses propres termes (« licéité du traitement », « droit à
l'effacement », « violation de données à caractère personnel »).
La recherche par similarité trouve alors mal les bons articles.

expand_query() ajoute les termes du règlement à la requête avant la
recherche, par exemple :
    « Quelles sont les bases légales d'un traitement ? »
devient
    « Quelles sont les bases légales d'un traitement ? (licéité du
    traitement) »
C'est déterministe et ne demande aucun appel au LLM.
"""

import re

# Pour chaque langue : (expressions courantes, terme du règlement).
# Les expressions sont en minuscules ; elles sont cherchées comme des
# mots entiers (« amende » ne correspond pas à « amendement »).
GLOSSARY = {
    "fr": [
        (
            (
                "base légale",
                "bases légales",
                "fondement juridique",
                "fondement légal",
                "base juridique",
            ),
            "licéité du traitement",
        ),
        (("droit à l'oubli",), "droit à l'effacement"),
        (
            (
                "fuite de données",
                "fuite des données",
                "piratage",
                "cyberattaque",
            ),
            "notification à l'autorité de contrôle d'une violation de données "
            "à caractère personnel",
        ),
        (
            (
                "durée de conservation",
                "conserver les données",
                "garder les données",
                "conserver des données",
                "garder des données",
            ),
            "limitation de la conservation",
        ),
        (("cnil",), "autorité de contrôle"),
        (("dpo",), "délégué à la protection des données"),
        (
            ("aipd", "pia", "étude d'impact"),
            "analyse d'impact relative à la protection des données",
        ),
        (
            ("amende", "amendes", "sanction", "sanctions"),
            "amendes administratives",
        ),
        (
            ("hors de l'ue", "hors ue", "hors de l'union européenne"),
            "transferts vers des pays tiers",
        ),
    ],
    "en": [
        (
            (
                "legal basis",
                "legal bases",
                "lawful basis",
                "lawful bases",
                "legal ground",
                "legal grounds",
            ),
            "lawfulness of processing",
        ),
        (("right to be forgotten",), "right to erasure"),
        (
            ("data leak", "leaked data", "hack", "hacked", "cyberattack"),
            "notification of a personal data breach to the supervisory "
            "authority",
        ),
        (
            ("retention period", "keep data", "store data"),
            "storage limitation",
        ),
        (("dpo",), "data protection officer"),
        (("dpia",), "data protection impact assessment"),
        (
            ("fine", "fines", "penalty", "penalties"),
            "administrative fines",
        ),
        (
            ("outside the eu", "outside the european union"),
            "transfers to third countries",
        ),
    ],
}


def contains_expression(text: str, expression: str) -> bool:
    """Vrai si « expression » apparaît dans « text » comme mot entier."""
    # \b : frontière de mot. re.escape protège les caractères spéciaux
    # (comme l'apostrophe ou le point).
    pattern = r"\b" + re.escape(expression) + r"\b"
    return re.search(pattern, text) is not None


def expand_query(query: str, language: str) -> str:
    """Ajoute à la requête les termes du règlement qui y correspondent."""
    text = query.lower()
    terms = []
    for expressions, regulation_term in GLOSSARY.get(language, []):
        # Terme déjà présent dans la requête : inutile de l'ajouter.
        if regulation_term in text or regulation_term in terms:
            continue
        for expression in expressions:
            if contains_expression(text, expression):
                terms.append(regulation_term)
                break

    if len(terms) == 0:
        return query
    return query + " (" + ", ".join(terms) + ")"
