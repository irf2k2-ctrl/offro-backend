"""Translated category names (additive; the English `name` stays the one stored on stores/deals/filters and sent in API requests).

A category document may carry `names` = {language code: name} (admin-editable, same shape as the City Master). Categories that
predate this have none, so the well-known built-in ones fall back to the glossary below until an admin sets their own translation.
Anything else is shown exactly as the admin typed it. The app always keeps using `name` for logic and shows `display_name`."""
from services.cities import clean_names, display_name as _city_display

# Built-in category glossary (fallback only; an admin-entered name always wins).
BUILTIN = {
    "ar": {
        "restaurant": "مطعم", "restaurants": "مطاعم", "grocery": "بقالة", "pharmacy": "صيدلية", "electronics": "إلكترونيات",
        "fashion": "أزياء", "bakery": "مخبز", "salon": "صالون", "fitness": "لياقة بدنية", "hospital": "مستشفى",
        "education": "تعليم", "automobile": "سيارات", "other": "أخرى", "cafe": "مقهى", "beauty": "جمال",
        "jewelry": "مجوهرات", "jewellery": "مجوهرات", "stationery": "قرطاسية", "books": "كتب", "travel": "سفر",
        "furniture": "أثاث", "sports": "رياضة", "toys": "ألعاب", "supermarket": "سوبر ماركت", "clinic": "عيادة",
    },
}


def clean(value) -> dict:
    return clean_names(value)


def category_display_name(doc: dict, lang: str = "") -> str:
    """The category's name in `lang`: admin translation, else the built-in glossary, else the English name."""
    english = str((doc or {}).get("name") or "")
    got = _city_display(doc, lang)
    if got != english or not lang:
        return got
    code = str(lang).strip().lower().replace("_", "-").split("-")[0]
    return BUILTIN.get(code, {}).get(english.strip().lower(), english)


def names_for(doc: dict) -> dict:
    """Stored `names` plus the built-in glossary entries for languages the admin has not filled in."""
    out = {}
    english = str((doc or {}).get("name") or "").strip().lower()
    for lang, table in BUILTIN.items():
        if english in table:
            out[lang] = table[english]
    stored = (doc or {}).get("names")
    if isinstance(stored, dict):
        for k, v in stored.items():
            if isinstance(v, str) and v.strip():
                out[str(k).lower()] = v.strip()
    return out
