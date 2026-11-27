"""Skill "семья": the family from photolib's tree - the household and close
relatives with exact birthdays in the prompt (refreshed hourly), the whole
tree (~170 people) through family_lookup."""
import photolib
from core.skills import Result, Skill, Tool
from skills.photos import remember, with_meta


def context():
    return "## Family\n\n" + photolib.family_block()


def lookup(args, person):
    return photolib.lookup(args["name"])


def _d(iso):
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if iso and len(iso) >= 10 else ""


def card(args, person):
    """A person on the TV: portrait, dates, relatives, their latest photos."""
    hits = photolib.find(args["name"])
    if not hits:
        return "В родовом дереве такого человека не нашлось."
    pid = hits[0]
    persons, relations = photolib.tree()
    p = photolib.person(pid)
    photos, total = photolib.person_photos(pid, limit=200)
    # the archive's own order is not by date: newest first here
    photos = sorted(photos, key=lambda ph: ph.get("photo_date") or "", reverse=True)[:48]
    rel = photolib.relatives(pid, persons, relations)
    name = lambda i: persons[i]["name"] if i in persons else "?"
    lines = []
    if p.get("birth_date"):
        lines.append(("около " if p.get("birth_date_approx") else "") + "родил(ась)ся " + _d(str(p["birth_date"])))
    if p.get("death_date"):
        lines.append("умер(ла) " + _d(str(p["death_date"])))
    for key, label in (("spouses", "супруг(а)"), ("parents", "родители"), ("children", "дети"), ("siblings", "братья и сёстры")):
        if rel[key]:
            lines.append(f"{label}: " + ", ".join(name(i) for i in rel[key]))
    items = [{"id": ph["image_id"], "caption": _d(ph.get("photo_date") or "")} for ph in photos]
    show = {"kind": "person", "name": p["name"], "face": p.get("cover_face_id"), "lines": lines,
            "photos": [it["id"] for it in items]}
    others = f" (есть ещё совпадения: {', '.join(name(i) for i in hits[1:4])})" if len(hits) > 1 else ""
    text = (f"Показала на экране карточку: {photolib.describe(pid, persons, relations)}. "
            f"Фото с ним/ней в архиве: {total}, на экране последние {len(items)}{others}.")
    remember(person, items)
    text = with_meta(text, items)        # who else is on them, when, where
    show["items"] = items
    return Result(text, show)


SKILL = Skill(
    name="family",
    summary="семья и родословная из photolib: кто кому кем приходится, даты, дни рождения",
    context=context,
    tools=[Tool(
        name="family_lookup",
        description=("Find a person in the family tree (photolib, ~170 people): birth and death dates, "
                     "parents, spouses, children, siblings, description. By name or part of it."),
        schema={"type": "object", "additionalProperties": False, "required": ["name"],
                "properties": {"name": {"type": "string", "description": "name or part of it"}}},
        handler=lookup,
        needs="persons.view",              # photolib section, as in its web UI
    ), Tool(
        name="person_card",
        description=("Show a person on the TV: portrait, dates, relatives and their latest archive photos "
                     "(numbered). For «покажи Соню», «покажи фото бабушки»."),
        schema={"type": "object", "additionalProperties": False, "required": ["name"],
                "properties": {"name": {"type": "string", "description": "name or part of it"}}},
        handler=card,
        needs="persons.view",
    )],
)
