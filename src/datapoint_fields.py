"""Shared logic for turning GenEval-negation metadata into datapoint fields.

Used by every script that writes to the Firestore `datapoints` collection
(add_datapoint.py, add_random_flux2_datapoints.py, select_and_upload_datapoints.py)
so the object/condition-building logic lives in exactly one place.

Each condition asks only about the positive visual predicate. Negation stays in
the internal `polarity`/`expected` fields, so a human answer can later be
compared with `expected` to determine prompt satisfaction.
"""


def build_objects(metadata):
    objects = []
    for inc in metadata.get("include", []):
        class_name = inc.get("class")
        if class_name and class_name not in objects:
            objects.append(class_name)
    return objects


def build_conditions(metadata):
    category = metadata.get("category")
    slots = metadata.get("slots", [])
    conditions = []

    if category in ("color", "material"):
        for i, slot in enumerate(slots):
            obj = slot["object"]
            value = slot["value"]
            polarity = slot["polarity"]
            question = (
                f"Does the {obj} appear {value}?"
                if category == "material"
                else f"Is the {obj} {value}?"
            )
            conditions.append(
                {
                    "id": f"cond_attr_{i}",
                    "type": "attribute",
                    "category": category,
                    "subject": obj,
                    "predicate": value,
                    "polarity": polarity,
                    "expected": polarity == "pos",
                    "question": question,
                }
            )
        return conditions

    # relation categories (comparative, directional, proximity, depth): the
    # subject is the include entry carrying "position" -> [relation_phrase, slot_idx]
    subject_entry = next(
        (inc for inc in metadata.get("include", []) if "position" in inc), None
    )
    if subject_entry is None:
        return conditions

    subject = subject_entry["class"]
    for i, (relation_phrase, slot_idx) in enumerate(subject_entry["position"]):
        target = slots[slot_idx]["target"]
        is_negated = relation_phrase.lower().startswith("not ")
        predicate = relation_phrase[4:] if is_negated else relation_phrase
        polarity = "neg" if is_negated else "pos"
        conditions.append(
            {
                "id": f"cond_rel_{i}",
                "type": "relation",
                "subject": subject,
                "predicate": predicate,
                "polarity": polarity,
                "expected": polarity == "pos",
                "target": target,
                "question": f"Is the {subject} {predicate} the {target}?",
            }
        )
    return conditions
