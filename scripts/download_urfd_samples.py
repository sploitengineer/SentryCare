"""Download official URFD camera-0 RGB sequences for academic fall-detection evaluation.

Default: the original four-clip smoke test. ``--falls N --adls M`` downloads more clips into a
separate manifest (``urfd_extended.csv``) so earlier smoke-test results stay comparable.
"""

import argparse

import csv
from pathlib import Path
import shutil
import stat
import urllib.request
import zipfile

from sentrycare.paths import DATA_DIR

BASE_URL = "https://fenix.ur.edu.pl/~mkepski/ds/data/"
SOURCE_PAGE = "https://fenix.ur.edu.pl/~mkepski/ds/uf.html"


def download(url, destination):
    if destination.is_file() and destination.stat().st_size:
        return
    partial = destination.with_suffix(destination.suffix + ".partial")
    with urllib.request.urlopen(url, timeout=60) as source, partial.open("wb") as target:
        shutil.copyfileobj(source, target)
    partial.replace(destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--falls", type=int, default=0, help="Fall clips to fetch (1-30); 0 = original four-clip set")
    parser.add_argument("--adls", type=int, default=0, help="Daily-activity clips to fetch (1-40)")
    args = parser.parse_args(argv)
    if not 0 <= args.falls <= 30 or not 0 <= args.adls <= 40:
        parser.error("URFD has 30 fall and 40 ADL sequences")
    root = DATA_DIR / "datasets/urfd_samples"
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    if args.falls or args.adls:
        # Clips 01-02 were used while developing the heuristics; mark them so reports can exclude them.
        clips = [(f"fall-{i:02d}", "dev" if i <= 2 else "test") for i in range(1, args.falls + 1)]
        clips += [(f"adl-{i:02d}", "dev" if i <= 2 else "test") for i in range(1, args.adls + 1)]
        manifest_name = "urfd_extended.csv"
    else:
        clips = (("fall-01", "train"), ("adl-01", "train"), ("fall-02", "validation"), ("adl-02", "validation"))
        manifest_name = "urfd_samples.csv"
    for name, split in clips:
        archive = root / f"{name}-cam0-rgb.zip"
        timing = root / f"{name}-data.csv"
        print(f"Downloading {name}", flush=True)
        download(BASE_URL + archive.name, archive)
        download(BASE_URL + timing.name, timing)
        destination = (root / f"{name}-cam0-rgb").resolve()
        destination.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination) or stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("Unsafe archive member rejected")
            zipped.extractall(destination)
        rows.append({"path": f"../datasets/urfd_samples/{destination.name}", "label": name.split("-")[0],
                     "split": split, "timestamps": f"../datasets/urfd_samples/{timing.name}"})
    output = DATA_DIR / "evaluation" / manifest_name
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["path", "label", "split", "timestamps"])
        writer.writeheader()
        writer.writerows(rows)
    (root / "ATTRIBUTION.md").write_text(
        f"Source: {SOURCE_PAGE}\n\nLicense: CC BY-NC-SA 4.0; non-commercial academic use.\n\n"
        "Citation: Bogdan Kwolek, Michal Kepski, Human fall detection on embedded platform using depth maps "
        "and wireless accelerometer, Computer Methods and Programs in Biomedicine, 117(3), 2014, 489-501.\n\n"
        "Only camera-0 RGB samples and their timing CSVs were downloaded. Splits are clip-level groupings "
        "(clips 01-02 were used during development); they are not subject-disjoint, so results are "
        "indicative rather than validated accuracy.\n", encoding="utf-8")
    print(f"Manifest: {output}", flush=True)


if __name__ == "__main__":
    main()
