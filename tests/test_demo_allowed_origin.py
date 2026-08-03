from __future__ import annotations

from app.config import resolve_demo_allowed_origin


def test_resolve_demo_allowed_origin_prefers_explicit() -> None:
    assert (
        resolve_demo_allowed_origin(
            explicit="https://demo.example.com/",
            render_external_url="https://ignored.onrender.com",
        )
        == "https://demo.example.com"
    )


def test_resolve_demo_allowed_origin_uses_render_url() -> None:
    assert (
        resolve_demo_allowed_origin(
            explicit="",
            render_external_url="https://rivet-public-demo.onrender.com",
        )
        == "https://rivet-public-demo.onrender.com"
    )


def test_resolve_demo_allowed_origin_uses_railway_domain() -> None:
    assert (
        resolve_demo_allowed_origin(
            explicit="",
            render_external_url="",
            railway_public_domain="my-app.up.railway.app",
        )
        == "https://my-app.up.railway.app"
    )


def test_resolve_demo_allowed_origin_defaults_local() -> None:
    assert (
        resolve_demo_allowed_origin(
            explicit="",
            render_external_url="",
            railway_public_domain="",
        )
        == "http://127.0.0.1:8000"
    )
