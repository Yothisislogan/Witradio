"""A synthetic cleared classical library for planner tests."""

import random

from mac.catalog import FileRecord, Play, Work

COMPOSERS = [
    "Johann Sebastian Bach", "Wolfgang Amadeus Mozart", "Ludwig van Beethoven", "Franz Schubert",
    "Johannes Brahms", "Frederic Chopin", "Robert Schumann", "Felix Mendelssohn", "Antonin Dvorak",
    "Pyotr Ilyich Tchaikovsky", "Joseph Haydn", "George Frideric Handel", "Antonio Vivaldi",
    "Claude Debussy", "Maurice Ravel", "Edvard Grieg", "Franz Liszt", "Gabriel Faure",
    "Camille Saint-Saens", "Anton Bruckner", "Gustav Mahler", "Jean Sibelius", "Carl Nielsen",
    "Sergei Rachmaninoff", "Modest Mussorgsky", "Nikolai Rimsky-Korsakov", "Hector Berlioz",
    "Georges Bizet", "Giuseppe Verdi", "Henry Purcell", "Claudio Monteverdi", "Arcangelo Corelli",
    "Domenico Scarlatti", "Georg Philipp Telemann", "Clara Schumann", "Fanny Mendelssohn",
    "Edward Elgar", "John Philip Sousa", "Erik Satie", "Gustav Holst",
]

SHAPES = [
    # (count, min_minutes, max_minutes, forms, extra tags)
    (220, 2, 8, ["prelude", "march", "nocturne", "dance", "song", "etude"], ["quiet", "brass", "keyboard"]),
    (480, 8, 30, ["sonata", "overture", "chamber", "quartet", "trio", "suite", "concerto", "serenade"],
     ["keyboard", "strings", "calm", "focus", "quiet"]),
    (80, 30, 40, ["concerto", "symphony", "sonata"], ["strings", "symphonic"]),
    (90, 40, 95, ["symphony", "mass", "oratorio", "variations", "requiem", "opera"], ["sacred", "choral", "symphonic"]),
]


def make_work(work_id: int, minutes: float, composer: str, form: str, tags=(), **extra) -> Work:
    files = (FileRecord(id=work_id, work_id=work_id, seq=1, path=f"/library/{work_id}.flac",
                        duration_sec=minutes * 60, lossless=extra.pop("lossless", True)),)
    return Work(
        id=work_id, kind=extra.pop("kind", "classical"), title=f"{form.title()} No. {work_id}",
        source="musopen", license="public_domain", credit_line=f"Performer {work_id} via Musopen",
        composer=composer, performer=f"Performer {work_id}", form=form, composition_year=1800,
        tags=tuple(tags), gain_db=-1.5, files=files, **extra,
    )


def make_library(seed: int = 7, scale: float = 1.0, beethoven_cycle: bool = True) -> list[Work]:
    rng = random.Random(seed)
    works: list[Work] = []
    next_id = 1
    for count, lo, hi, forms, tags in SHAPES:
        for _ in range(int(count * scale)):
            minutes = round(rng.uniform(lo, hi), 2)
            picked_tags = [t for t in tags if rng.random() < 0.3]
            works.append(make_work(next_id, minutes, rng.choice(COMPOSERS), rng.choice(forms), picked_tags))
            next_id += 1
    if beethoven_cycle:
        for n in range(1, 33):
            works.append(make_work(
                next_id, round(rng.uniform(10, 38), 2), "Ludwig van Beethoven", "sonata", ["keyboard"],
                cycle="beethoven_piano_sonatas", cycle_no=n,
            ))
            next_id += 1
    return works


def hours(works) -> float:
    return sum(w.duration_sec for w in works) / 3600
