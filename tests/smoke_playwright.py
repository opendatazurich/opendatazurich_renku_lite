"""
Headless end-to-end smoke test: open starter notebooks via ?package_id=&resource_id=,
run all cells in the in-browser Pyodide kernel and fail on any error output.

    docker run -d -p 18080:8080 odz-lite
    uv run playwright install chromium
    uv run python tests/smoke_playwright.py http://localhost:18080
"""

import asyncio
import re
import sys
import time

from playwright.async_api import Page, async_playwright

DATASETS = [
    # (label, package_id, resource_id)
    ("csv, default dataset", "bau_hae_lima_zuordnung_adr_quartier_bzo16_bzo99_od5143", None),
    (
        "parquet via proxy",
        "politik_abstimmungen_seit1933",
        "3e87b102-f19c-47f4-ab50-a679b51cf77e",
    ),
    (
        "csv via proxy",
        "politik_abstimmungen_seit1933",
        "65e011cf-6479-4fea-aa12-5928924ac4d2",
    ),
    (
        "geo via WFS",
        "geo_oeffentlich_zugaengliche_parkplaetze_dav",
        "9d4f3dfc-e998-4c23-ba4e-6dde97a4ee16",
    ),
]
RUN_TIMEOUT_S = 600
PRELOADED_WHEEL = "/pypi/opendata_lite-1.0.0-py3-none-any.whl"
PYPI_HOSTS = r"^https://(pypi\.org|files\.pythonhosted\.org)/"
ERROR_PATTERN = re.compile(
    r"Traceback \(most recent call last\)|^\w*(Error|Exception):", re.MULTILINE
)


async def run_all(page: Page) -> None:
    await page.locator(".lm-MenuBar-itemLabel", has_text=re.compile(r"^Run$")).click()
    await page.locator(".lm-Menu-itemLabel", has_text=re.compile(r"^Run All Cells$")).click()


async def wait_until_done(page: Page) -> None:
    prompts = page.locator(".jp-NotebookPanel:visible .jp-CodeCell .jp-InputPrompt")
    deadline = time.monotonic() + RUN_TIMEOUT_S
    seen_busy = False
    while time.monotonic() < deadline:
        texts = [t.strip() for t in await prompts.all_inner_texts()]
        busy = any("[*]" in t for t in texts)
        seen_busy = seen_busy or busy
        all_numbered = bool(texts) and all(re.search(r"\[\d+\]", t) for t in texts)
        if not busy and (seen_busy or all_numbered):
            return
        await page.wait_for_timeout(2000)
    raise TimeoutError(f"cells still running after {RUN_TIMEOUT_S}s")


async def smoke(browser, base: str, label: str, package_id: str, resource_id: str | None) -> list:
    context = await browser.new_context()  # fresh browser storage per dataset
    requests = []
    context.on("request", lambda request: requests.append(request.url))
    page = await context.new_page()
    query = f"package_id={package_id}" + (f"&resource_id={resource_id}" if resource_id else "")
    started = time.monotonic()
    problems = []
    try:
        await page.goto(f"{base}/lab/index.html?{query}")
        await page.wait_for_selector(".jp-NotebookPanel .jp-CodeCell", timeout=60_000)
        await page.wait_for_selector(".jp-Notebook-ExecutionIndicator", timeout=60_000)
        await run_all(page)
        await wait_until_done(page)

        prompts = [
            t.strip() for t in await page.locator(".jp-CodeCell .jp-InputPrompt").all_inner_texts()
        ]
        not_run = [i for i, t in enumerate(prompts) if not re.search(r"\[\d+\]", t)]
        if not_run:
            problems.append(f"code cells not executed: {not_run}")
        outputs = await page.locator(".jp-OutputArea-output").all_inner_texts()
        errors = [o for o in outputs if ERROR_PATTERN.search(o)]
        problems += [f"error output:\n{e[-1500:]}" for e in errors]
        # The helper and pure-Python wheels are preloaded from the site, never from PyPI.
        if not any(url.endswith(PRELOADED_WHEEL) for url in requests):
            problems.append(f"{PRELOADED_WHEEL} was not preloaded")
        pypi = [url for url in requests if re.search(PYPI_HOSTS, url)]
        if pypi:
            problems.append(f"requests to PyPI: {pypi}")
        images = await page.locator(".jp-OutputArea-output img").count()
        print(f"  {len(prompts)} code cells | {images} plots")
    except Exception as error:  # noqa: BLE001 - report and continue with the next dataset
        problems.append(f"{type(error).__name__}: {error}")
        await page.screenshot(path=f"smoke-{package_id}.png", full_page=True)
    finally:
        await context.close()
    status = "FAIL" if problems else "ok"
    print(f"{status:4} {label} ({package_id}) in {time.monotonic() - started:.0f}s")
    for problem in problems:
        print("     " + problem.replace("\n", "\n     "))
    return problems


async def launcher_checks(browser, base: str) -> list:
    """Bad parameters show an error dialog; a revisit reopens the edited copy."""
    problems = []
    context = await browser.new_context()
    page = await context.new_page()
    for query, expected in (
        ("package_id=../../etc", "Invalid package_id"),
        ("package_id=does_not_exist_xyz", "was not found"),
        ("package_id=politik_abstimmungen_seit1933&resource_id=1;import os", "Invalid resource_id"),
    ):
        await page.goto(f"{base}/lab/index.html?{query}")
        dialog = page.locator(".jp-Dialog-content")
        try:
            await dialog.wait_for(timeout=30_000)
            if expected not in await dialog.inner_text():
                problems.append(f"{query}: dialog says {await dialog.inner_text()!r}")
        except Exception:  # noqa: BLE001
            problems.append(f"{query}: no error dialog")

    package_id = DATASETS[0][1]
    await page.goto(f"{base}/lab/index.html?package_id={package_id}")
    cell = page.locator(".jp-NotebookPanel .jp-MarkdownCell").first
    await cell.wait_for(timeout=60_000)
    if "package_id" in page.url:
        problems.append(f"query not removed from URL: {page.url}")
    # Edit the notebook, save, then launch the same dataset again.
    await page.locator(".jp-NotebookPanel .jp-CodeCell .cm-content").first.click()
    await page.keyboard.press("ControlOrMeta+End")
    await page.keyboard.type("\n# edited by smoke test")
    await page.keyboard.press("ControlOrMeta+s")
    await page.wait_for_timeout(1500)
    await page.goto(f"{base}/lab/index.html?package_id={package_id}")
    await page.locator(".jp-NotebookPanel .jp-CodeCell").first.wait_for(timeout=60_000)
    if "# edited by smoke test" not in await page.locator(".jp-NotebookPanel").inner_text():
        problems.append("revisit did not keep the edited notebook")
    await context.close()
    print(
        f"{'FAIL' if problems else 'ok':4} launcher: error dialogs, URL cleanup, revisit keeps edits"
    )
    for problem in problems:
        print("     " + problem)
    return problems


async def main(base: str) -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        results = [await launcher_checks(browser, base)]
        results += [await smoke(browser, base, *dataset) for dataset in DATASETS]
        await browser.close()
    failed = sum(bool(r) for r in results)
    print(f"{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:18080")))
