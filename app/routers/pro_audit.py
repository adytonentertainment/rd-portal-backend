"""
PRO Audit Router - ASCAP/BMI Registration Verification
Working version with proper popup handling - tested locally with visible browser
"""

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
import asyncio
import json
import re

pro_audit_router = APIRouter(prefix="/api/audit", tags=["PRO Audit"])


class SongInput(BaseModel):
    title: str
    performer: Optional[str] = None


class AuditRequest(BaseModel):
    writer_name: Optional[str] = None
    writer_ipi: Optional[str] = None
    publisher_name: Optional[str] = None
    publisher_ipi: Optional[str] = None
    songs: List[SongInput]


# IPRoyal proxy config - Spain location (disabled - subscription expired)
PROXY = {
    "server": "http://geo.iproyal.com:12321",
    "username": "v8IAMGQiBHyYW0jU",
    "password": "0S3ZqAnE97YbsJ2U_country-es_session-{session}_lifetime-30m"
}

# Set to False to disable proxy (for local testing or when proxy is expired)
USE_PROXY = False

playwright_instance = None


def normalize_name(name: str) -> str:
    name = re.sub(r'[^\w\s]', ' ', name.lower())
    business_suffixes = [
        'publishing', 'pub', 'music', 'entertainment', 'records', 'recordings',
        'llc', 'inc', 'corp', 'corporation', 'ltd', 'limited', 'co', 'company',
        'sl', 'sa', 'gmbh', 'bv', 'nv', 'ag', 'plc',
        'group', 'holdings', 'international', 'worldwide', 'global',
        'productions', 'prod', 'studios', 'studio', 'media', 'enterprises'
    ]
    parts = name.split()
    filtered_parts = [p for p in parts if p not in business_suffixes]
    if not filtered_parts:
        filtered_parts = parts
    return ' '.join(sorted(filtered_parts))


def fuzzy_name_match(search_name: str, result_text: str) -> bool:
    if not search_name:
        return None
    search_normalized = normalize_name(search_name)
    search_parts = set(search_normalized.split())
    meaningful_parts = [p for p in search_parts if len(p) > 2]
    if not meaningful_parts:
        meaningful_parts = list(search_parts)
    result_lower = result_text.lower()
    if search_normalized in normalize_name(result_text):
        return True
    if len(meaningful_parts) >= 1:
        matches = sum(1 for part in meaningful_parts if part in result_lower)
        if matches == len(meaningful_parts):
            return True
    if len(meaningful_parts) == 2:
        parts_list = list(meaningful_parts)
        reversed_name = f"{parts_list[1]} {parts_list[0]}"
        if reversed_name in result_lower:
            return True
    return False


def check_ipi_in_results(ipi: str, result_text: str) -> bool:
    if not ipi:
        return None
    clean_ipi = re.sub(r'\D', '', ipi)
    clean_ipi_no_zeros = clean_ipi.lstrip('0')
    if clean_ipi in result_text:
        return True
    if clean_ipi_no_zeros and clean_ipi_no_zeros in result_text:
        return True
    if ipi in result_text:
        return True
    ipi_patterns = [
        rf'IPI[:\s#]*{clean_ipi}',
        rf'IPI[:\s#]*{clean_ipi_no_zeros}',
        rf'CAE[:\s#/]*{clean_ipi}',
        rf'CAE[:\s#/]*{clean_ipi_no_zeros}',
    ]
    for pattern in ipi_patterns:
        if re.search(pattern, result_text, re.IGNORECASE):
            return True
    return False


def analyze_registration(
    writer_name: Optional[str],
    writer_ipi: Optional[str],
    publisher_name: Optional[str],
    publisher_ipi: Optional[str],
    results: List[dict],
    performer: Optional[str] = None
) -> dict:
    if not results:
        return {
            'status': 'not_registered',
            'writer_name_found': None,
            'writer_ipi_found': None,
            'publisher_name_found': None,
            'publisher_ipi_found': None,
            'performer_found': None,
            'message': 'Song not found in database'
        }

    all_text = ' '.join([r.get('raw', '') for r in results])

    performer_found = fuzzy_name_match(performer, all_text) if performer else None
    writer_name_found = fuzzy_name_match(writer_name, all_text) if writer_name else None
    writer_ipi_found = check_ipi_in_results(writer_ipi, all_text) if writer_ipi else None
    publisher_name_found = fuzzy_name_match(publisher_name, all_text) if publisher_name else None
    publisher_ipi_found = check_ipi_in_results(publisher_ipi, all_text) if publisher_ipi else None

    issues = []
    registered_items = []

    if writer_name:
        if writer_ipi:
            if writer_name_found and writer_ipi_found:
                registered_items.append("Writer (name + IPI)")
            elif writer_name_found:
                issues.append("Writer name found but IPI missing")
            else:
                issues.append("Writer not found")
        else:
            if writer_name_found:
                registered_items.append("Writer (name)")
            else:
                issues.append("Writer not found")

    if publisher_name:
        if publisher_ipi:
            if publisher_name_found and publisher_ipi_found:
                registered_items.append("Publisher (name + IPI)")
            elif publisher_name_found:
                issues.append("Publisher name found but IPI missing")
            else:
                issues.append("Publisher not found")
        else:
            if publisher_name_found:
                registered_items.append("Publisher (name)")
            else:
                issues.append("Publisher not found")

    if len(issues) == 0 and len(registered_items) > 0:
        status = 'registered'
        message = 'Properly registered: ' + ', '.join(registered_items)
    elif len(registered_items) > 0:
        status = 'collection_issue'
        message = '; '.join(issues)
    else:
        status = 'collection_issue'
        message = '; '.join(issues) if issues else 'Not associated with this registration'

    return {
        'status': status,
        'writer_name_found': writer_name_found,
        'writer_ipi_found': writer_ipi_found,
        'publisher_name_found': publisher_name_found,
        'publisher_ipi_found': publisher_ipi_found,
        'performer_found': performer_found,
        'message': message
    }


async def get_playwright():
    global playwright_instance
    if playwright_instance is None:
        from playwright.async_api import async_playwright
        playwright_instance = await async_playwright().start()
    return playwright_instance


async def dismiss_popup(page, popup_type: str) -> bool:
    """Dismiss a specific popup type"""
    if popup_type == "trustarc":
        for frame in page.frames:
            if "trustarc" in (frame.url or ""):
                try:
                    btn = frame.locator("a.call")
                    if await btn.count() > 0:
                        await btn.first.click(timeout=2000)
                        return True
                except:
                    pass
    elif popup_type == "agree":
        try:
            agree = page.get_by_role("button", name="I AGREE")
            if await agree.count() > 0 and await agree.first.is_visible():
                await agree.first.click(timeout=2000)
                return True
        except:
            pass
    elif popup_type == "skip":
        try:
            skip = page.get_by_role("button", name="SKIP")
            if await skip.count() > 0 and await skip.first.is_visible():
                await skip.first.click(timeout=2000)
                return True
        except:
            pass
    return False


async def search_ascap_title(title: str, performer: Optional[str] = None) -> dict:
    """Search ASCAP ACE database with proper popup handling"""
    import random
    browser = None
    context = None
    page = None
    try:
        from playwright_stealth import Stealth

        pw = await get_playwright()

        browser = await pw.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
                '--disable-gpu',
            ]
        )

        # Build context options
        context_options = {
            'viewport': {'width': 1400, 'height': 900},
            'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'locale': 'en-US',
            'timezone_id': 'America/New_York',
        }

        # Only add proxy if enabled
        if USE_PROXY:
            session_id = f"search{random.randint(10000, 99999)}"
            context_options['proxy'] = {
                "server": PROXY["server"],
                "username": PROXY["username"],
                "password": PROXY["password"].format(session=session_id)
            }
            print(f"[ASCAP] Using proxy", flush=True)
        else:
            print(f"[ASCAP] Running without proxy", flush=True)

        context = await browser.new_context(**context_options)
        page = await context.new_page()

        stealth = Stealth(navigator_platform_override='MacIntel')
        await stealth.apply_stealth_async(page)

        print(f"[ASCAP] Searching: {title} / {performer}", flush=True)

        # Load page
        await page.goto('https://www.ascap.com/repertory#/', wait_until='domcontentloaded', timeout=90000)
        await page.wait_for_timeout(5000)

        # === DISMISS ALL POPUPS BEFORE SEARCH ===
        
        # TrustArc consent
        for _ in range(10):
            if await dismiss_popup(page, "trustarc"):
                print("[ASCAP] TrustArc dismissed", flush=True)
                await page.wait_for_timeout(3000)
                break
            await page.wait_for_timeout(500)

        # I AGREE
        for _ in range(20):
            if await dismiss_popup(page, "agree"):
                print("[ASCAP] I AGREE dismissed", flush=True)
                await page.wait_for_timeout(2000)
                break
            await page.wait_for_timeout(500)

        # SKIP tour
        for _ in range(10):
            if await dismiss_popup(page, "skip"):
                print("[ASCAP] SKIP dismissed", flush=True)
                await page.wait_for_timeout(2000)
                break
            await page.wait_for_timeout(500)

        await page.wait_for_timeout(2000)

        # === SEARCH ===
        title_input = page.locator('input[name="searchTerm"]:visible')
        await title_input.click()
        await page.wait_for_timeout(500)
        await title_input.type(title, delay=150)
        await page.wait_for_timeout(800)

        if performer:
            perf_input = page.locator('input[name="searchTermTwo"]:visible')
            await perf_input.click()
            await page.wait_for_timeout(500)
            await perf_input.type(performer, delay=130)
            await page.wait_for_timeout(1200)

        await page.locator("button.c-btn.c-btn--size-full:visible").first.click()
        print("[ASCAP] Search clicked", flush=True)
        await page.wait_for_timeout(10000)

        # Post-search popups
        for _ in range(10):
            d1 = await dismiss_popup(page, "agree")
            d2 = await dismiss_popup(page, "skip")
            if d1:
                print("[ASCAP] Post-search I AGREE", flush=True)
            if d2:
                print("[ASCAP] Post-search SKIP", flush=True)
            if not d1 and not d2:
                break
            await page.wait_for_timeout(1000)

        await page.wait_for_timeout(5000)

        # Extract results
        results = []
        body_text = await page.inner_text('body')

        # Check for results
        if "nothing matched" in body_text.lower() or "oh no" in body_text.lower():
            return {'status': 'not_found', 'count': 0, 'results': []}

        results.append({'raw': body_text[:10000]})
        return {'status': 'found', 'count': 1, 'results': results}

    except Exception as e:
        print(f"[ASCAP] Error: {str(e)}", flush=True)
        return {'status': 'error', 'error': str(e)[:200], 'results': []}
    finally:
        try:
            if page:
                await page.close()
        except:
            pass
        try:
            if context:
                await context.close()
        except:
            pass
        try:
            if browser:
                await browser.close()
        except:
            pass


@pro_audit_router.post("/stream")
async def audit_catalog_stream(request: AuditRequest):
    """Audit songs with real-time progress updates via SSE"""

    async def generate():
        results = []
        total = len([s for s in request.songs if s.title.strip()])

        yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

        for idx, song_input in enumerate(request.songs):
            if not song_input.title.strip():
                continue

            title = song_input.title.strip()
            performer = song_input.performer.strip() if song_input.performer else None
            display_name = f"{title}" + (f" ({performer})" if performer else "")

            yield f"data: {json.dumps({'type': 'progress', 'current': idx, 'total': total, 'song': display_name, 'status': 'searching'})}\n\n"

            ascap_result = await search_ascap_title(title, performer)

            analysis = analyze_registration(
                writer_name=request.writer_name,
                writer_ipi=request.writer_ipi,
                publisher_name=request.publisher_name,
                publisher_ipi=request.publisher_ipi,
                results=ascap_result.get('results', []),
                performer=performer
            )

            if ascap_result.get('status') != 'found':
                status = 'not_registered'
                analysis['status'] = 'not_registered'
                analysis['message'] = 'Song not found in PRO database'
            else:
                status = analysis['status']

            song_result = {
                'title': title,
                'performer': performer,
                'status': status,
                'analysis': analysis,
                'ascap': ascap_result
            }
            results.append(song_result)

            yield f"data: {json.dumps({'type': 'song_complete', 'current': idx, 'total': total, 'song': song_result})}\n\n"

            await asyncio.sleep(0.5)

        registered = sum(1 for r in results if r['status'] == 'registered')
        collection_issue = sum(1 for r in results if r['status'] == 'collection_issue')
        not_registered = sum(1 for r in results if r['status'] == 'not_registered')

        final_result = {
            "type": "complete",
            "user": {
                "writer_name": request.writer_name,
                "writer_ipi": request.writer_ipi,
                "publisher_name": request.publisher_name,
                "publisher_ipi": request.publisher_ipi
            },
            "summary": {
                "total": len(results),
                "registered": registered,
                "collection_issue": collection_issue,
                "not_registered": not_registered
            },
            "songs": results,
            "status": "success"
        }
        yield f"data: {json.dumps(final_result)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@pro_audit_router.get("/health")
async def pro_audit_health():
    return {"status": "ok", "service": "PRO Audit", "mode": "integrated", "version": "working-visible-local"}
