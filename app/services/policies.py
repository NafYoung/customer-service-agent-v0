from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast

from app.schemas import PolicySearchHit, PolicySearchResponse


class PolicyService:
    """Small deterministic policy search used by v0.

    This is intentionally not presented as semantic RAG. It provides a stable
    contract and citations now; a vector or hybrid retriever can replace the
    implementation later without changing the tool schema.
    """

    def __init__(
        self,
        policy_dir: Path,
        *,
        policy_documents: Mapping[str, str] | None = None,
    ):
        self.policy_dir = policy_dir
        documents = (
            dict(policy_documents)
            if policy_documents is not None
            else {
                path.name: path.read_text(encoding="utf-8")
                for path in sorted(policy_dir.iterdir())
                if path.is_file()
            }
        )
        index_payload = json.loads(documents["index.json"])
        if not isinstance(index_payload, list):
            raise ValueError("Policy index must be a JSON array.")
        self.index = cast(list[dict[str, object]], index_payload)
        self._policy_bodies = {
            str(entry["file"]): documents[str(entry["file"])]
            for entry in self.index
        }

    def search(
        self,
        *,
        query: str,
        region: str = "CN",
        channel: str = "ONLINE",
        top_k: int = 3,
    ) -> PolicySearchResponse:
        normalized_query = query.casefold()
        hits: list[PolicySearchHit] = []

        for entry in self.index:
            if entry["region"] != region or entry["channel"] != channel:
                continue

            keywords = [
                str(word).casefold()
                for word in cast(Iterable[object], entry.get("keywords", []))
            ]
            score = sum(3 for keyword in keywords if keyword in normalized_query)
            title = str(entry["title"])
            if title.casefold() in normalized_query:
                score += 5

            body = self._policy_bodies[str(entry["file"])]
            for term in normalized_query.split():
                if len(term) >= 2 and term in body.casefold():
                    score += 1

            if score <= 0:
                continue

            compact_body = " ".join(
                line.strip()
                for line in body.splitlines()
                if line.strip() and not line.startswith("#")
            )
            hits.append(
                PolicySearchHit(
                    policy_id=str(entry["policy_id"]),
                    title=title,
                    version=str(entry["version"]),
                    effective_date=str(entry["effective_date"]),
                    score=score,
                    excerpt=compact_body[:360],
                )
            )

        hits.sort(key=lambda hit: (-hit.score, hit.policy_id))
        return PolicySearchResponse(hits=hits[:top_k])
