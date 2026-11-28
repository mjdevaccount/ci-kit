"""ci-http-clients must fire: a new hand-built client (constructor, module helper, library import)
-> 1, a grandfathered file that grows -> 1, the seam and a baselined file -> 0, a dir under its
floor or a file that will not parse -> 2."""
from __future__ import annotations

from _pylint_repo import again, pads, run
from conftest import write

ARGS = ("--dir", "app:2", "--seam", "app/adapters/_http.py", "--use", "app.adapters._http.client()")
ONE = "import httpx\n\n\ndef fetch(u):\n    with httpx.Client(timeout=5) as c:\n        return c.get(u)\n"


def _files(**extra: str) -> dict[str, str]:
    return {**pads("app", 2), **extra}


def test_a_new_client_fires_and_names_the_seam(new_repo) -> None:
    aliased = "from httpx import AsyncClient as AC\n\n\nasync def f(u):\n    async with AC() as c:\n        return await c.get(u)\n"
    _, r = run(new_repo, "http_clients", _files(**{"app/fetcher.py": aliased}), *ARGS)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NEW: app/fetcher.py builds 1 HTTP client(s)" in r.stdout and "app.adapters._http.client()" in r.stdout + r.stderr


def test_a_module_helper_and_a_library_import_fire(new_repo) -> None:
    helper = "import httpx\nfrom httpx import post as hpost\n\n\ndef h(u):\n    return httpx.get(u).status_code, hpost(u)\n"
    lib = "import urllib.request\n\n\ndef f(u):\n    return urllib.request.urlopen(u).read()\n"
    _, r = run(new_repo, "http_clients", _files(**{"app/fetcher.py": helper, "app/feed.py": lib}), *ARGS)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NEW: app/fetcher.py builds 2 HTTP client(s)" in r.stdout and "NEW: app/feed.py builds 1" in r.stdout


def test_the_seam_and_a_baselined_file_pass_and_growth_fails(new_repo) -> None:
    seam = "import httpx\nimport requests\n\nA = httpx.Client()\nB = httpx.AsyncClient()\n"
    files = _files(**{"app/adapters/_http.py": seam, "app/fetcher.py": ONE,
                      "ci/http_clients_baseline.txt": "# debt\napp/fetcher.py|1\n",
                      "app/prose.py": '"""httpx.Client( in a docstring is not a client."""\nimport urllib.parse\n'})
    repo, r = run(new_repo, "http_clients", files, *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    write(repo / "app/fetcher.py", ONE + "B = httpx.Client(timeout=9)\n")
    r = again("http_clients", repo, *ARGS)
    assert r.returncode == 1 and "GREW: app/fetcher.py now 2 hand-built HTTP client(s), baseline 1" in r.stdout, r.stdout


def test_a_dir_under_its_floor_and_an_unparseable_file_are_2(new_repo) -> None:
    repo, r = run(new_repo, "http_clients", pads("app", 1), *ARGS)
    assert r.returncode == 2, r.stdout + r.stderr
    write(repo / "app/pad_1.py", "import httpx\ndef broken(:\n")
    from conftest import git
    git(repo, "add", "-A")
    r = again("http_clients", repo, *ARGS)
    assert r.returncode == 2 and "app/pad_1.py" in r.stderr, r.stdout + r.stderr
