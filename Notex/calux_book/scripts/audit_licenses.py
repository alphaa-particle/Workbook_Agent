from __future__ import annotations

import importlib.metadata as md
import json
import re
import sys
from pathlib import Path

try:
    import tomllib
except Exception:  # pragma: no cover
    tomllib = None

ALLOWED = {
    "apache software license",
    "apache-2.0",
    "mit license",
    "mit",
    "bsd license",
    "bsd-2-clause",
    "bsd-3-clause",
    "isc license",
    "isc",
    "python software foundation license",
    "psf",
}

DENY_PATTERNS = [
    re.compile(r"\bagpl\b", re.IGNORECASE),
    re.compile(r"\bsspl\b", re.IGNORECASE),
    re.compile(r"\bgpl\b", re.IGNORECASE),
]


def normalize_license_text(raw: str) -> str:
    return " ".join((raw or "").strip().lower().split())


def get_declared_license(name: str) -> str:
    try:
        meta = md.metadata(name)
    except Exception:
        return "unknown"

    license_field = normalize_license_text(meta.get("License", ""))
    classifiers = [
        normalize_license_text(v)
        for v in meta.get_all("Classifier", [])
        if "license" in v.lower()
    ]

    if license_field and license_field != "unknown":
        return license_field
    if classifiers:
        return "; ".join(classifiers)
    return "unknown"


def _normalize_dep_name(raw: str) -> str:
    dep = raw.strip()
    dep = dep.split(";", 1)[0].strip()
    dep = re.split(r"[<>=!~\[]", dep, maxsplit=1)[0].strip()
    return dep


def load_declared_dependencies() -> list[str]:
    deps: list[str] = []
    pyproject = Path("pyproject.toml")
    if tomllib is not None and pyproject.exists():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data.get("project", {})
        declared = project.get("dependencies", []) or []
        deps.extend(_normalize_dep_name(dep) for dep in declared)

    # Merge requirements.txt for environments that rely on pip install -r
    req = Path("requirements.txt")
    if req.exists():
        for line in req.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            deps.append(_normalize_dep_name(stripped))

    # Dedup preserving order
    out: list[str] = []
    seen: set[str] = set()
    for dep in deps:
        key = dep.lower().replace("_", "-")
        if not dep or key in seen:
            continue
        seen.add(key)
        out.append(dep)
    return out


def classify(license_text: str) -> str:
    if not license_text or license_text == "unknown":
        return "unknown"
    permissive_hint = any(ok in license_text for ok in ALLOWED)
    for pattern in DENY_PATTERNS:
        if pattern.search(license_text):
            # Some packages declare "GPL-compatible BSD" or similar wording.
            # If a clear permissive marker is present, keep for manual review.
            if permissive_hint:
                return "review"
            return "denied"
    if permissive_hint:
        return "allowed"
    return "review"


def _find_distribution_name(dep_name: str) -> str | None:
    key = dep_name.lower().replace("_", "-")
    for dist in md.distributions():
        name = dist.metadata.get("Name", "")
        if not name:
            continue
        if name.lower().replace("_", "-") == key:
            return name
    return None


def main() -> int:
    report: list[dict[str, str]] = []
    denied = 0
    review = 0

    deps = load_declared_dependencies()
    for dep in sorted(deps, key=lambda d: d.lower()):
        resolved_name = _find_distribution_name(dep)
        if resolved_name is None:
            name = dep
            version = "not-installed"
            license_text = "unknown"
            status = "unknown"
            review += 1
            report.append({
                "name": name,
                "version": version,
                "license": license_text,
                "status": status,
            })
            continue

        name = resolved_name
        try:
            version = md.version(name)
        except Exception:
            version = "unknown"
        license_text = get_declared_license(name)
        status = classify(license_text)
        if status == "denied":
            denied += 1
        elif status in {"review", "unknown"}:
            review += 1

        report.append({
            "name": name,
            "version": version,
            "license": license_text,
            "status": status,
        })

    out_dir = Path("data")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "license_audit.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Wrote license report: {out_path}")
    print(f"denied={denied} review_or_unknown={review} total={len(report)}")

    if denied > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
