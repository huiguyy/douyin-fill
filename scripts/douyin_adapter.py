"""Douyin form shell; all clicks are mediated by fillup_core.policy."""
from __future__ import annotations

from typing import Any

from fillup_core.contract import FillRequest
from fillup_core.errors import FillError
from fillup_core.page import (
    append_tags,
    check_account_state,
    close_date_picker,
    dismiss_known_overlay,
    fill_input,
    fill_rich_text,
    first_visible,
    require_visible,
    wait_for_upload,
)
from fillup_core.pace import apace
from fillup_core.policy import guarded_click

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
_TITLE_SELECTORS = (
    'input[placeholder*="填写作品标题"]',
    'input[placeholder*="作品标题"]',
    'input[placeholder*="标题"]',
    ".title-input input",
)
_DESC_SELECTORS = (
    '.editor-kit-editor-container [contenteditable="true"]',
    'div.zone-container[contenteditable="true"]',
    'div[data-placeholder*="简介"]',
    'div[contenteditable="true"]',
    'textarea[placeholder*="简介"]',
)


async def _set_video(page: Any, request: FillRequest) -> None:
    selectors = (
        "input.upload-btn-input",
        'input[type="file"][accept*="video"]',
        "div[class^='container'] input[accept]",
        'input[type="file"]',
    )
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="attached", timeout=8000)
            await locator.set_input_files(str(request.video()), timeout=60000)
            return
        except Exception:
            continue
    upload_button = await first_visible(
        page,
        ('button:has-text("上传视频")', 'button:has-text("上传")', '[role="button"]:has-text("上传")'),
        timeout=2500,
    )
    if upload_button is None:
        raise FillError("upload_control_not_found", "Could not find Douyin's video upload control.", retryable=True)
    try:
        async with page.expect_file_chooser(timeout=6000) as chooser_info:
            await guarded_click(upload_button, intent="choose_media")
        chooser = await chooser_info.value
        await chooser.set_files(str(request.video()))
    except FillError:
        raise
    except Exception as exc:
        raise FillError("upload_failed", f"Could not attach the video: {exc.__class__.__name__}", retryable=True) from exc


async def _description_editor(page: Any, timeout_seconds: int) -> Any:
    timeout = min(180, max(30, timeout_seconds)) * 1000
    for selector in _DESC_SELECTORS:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            return locator
        except Exception:
            continue
    raise FillError(
        "editor_not_found",
        "The description editor did not appear after upload. The video may still be processing "
        "or the platform selectors may have changed.",
        retryable=True,
    )


async def _set_cover(page: Any, request: FillRequest) -> None:
    await page.keyboard.press("Escape")
    await dismiss_known_overlay(page)
    opener = None
    for label in ("编辑封面", "选择封面", "设置封面"):
        candidate = page.get_by_text(label, exact=True).first
        try:
            if await candidate.count() and await candidate.is_visible():
                opener = candidate
                break
        except Exception:
            continue
    if opener is None:
        opener = await first_visible(page, ('[class*="cover-"] img', '[class*="cover"]'), timeout=2500)
    if opener is None:
        raise FillError("cover_control_not_found", "Could not find Douyin's cover editor.", retryable=True)
    await guarded_click(opener, intent="open_cover")
    modal = await require_visible(
        page,
        ("div.dy-creator-content-modal", ".semi-modal-content"),
        "cover dialog",
        timeout=8000,
    )
    portrait_tab = modal.get_by_text("设置竖封面", exact=True).first
    try:
        if await portrait_tab.count() and await portrait_tab.is_visible():
            await guarded_click(portrait_tab, intent="select_cover_tab")
    except Exception:
        pass
    cover_input = modal.locator(
        ".semi-upload:has(.semi-upload-drag-area-main-text) input.semi-upload-hidden-input"
    ).first
    if not await cover_input.count():
        cover_input = modal.locator('input[type="file"]').last
    try:
        await cover_input.wait_for(state="attached", timeout=10000)
        await cover_input.set_input_files(str(request.cover()), timeout=60000)
    except Exception as exc:
        raise FillError("cover_upload_failed", f"Could not attach the cover: {exc.__class__.__name__}", retryable=True) from exc

    finish = modal.get_by_role("button", name="完成", exact=True).first
    enabled = False
    for _ in range(60):
        try:
            if await finish.count() and await finish.is_enabled():
                enabled = True
                break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    if not enabled:
        raise FillError("cover_processing_timeout", "The cover Finish button did not become enabled.", retryable=True)
    await guarded_click(finish, intent="confirm_cover")
    await page.wait_for_timeout(800)
    for label in ("仍然完成", "仍要完成", "确定", "确认", "继续"):
        confirm = page.get_by_role("button", name=label, exact=True).first
        try:
            if await confirm.count() and await confirm.is_visible():
                await guarded_click(confirm, intent="confirm_cover")
                break
        except Exception:
            continue
    try:
        await modal.wait_for(state="hidden", timeout=15000)
    except Exception as exc:
        raise FillError(
            "cover_dialog_open",
            "The cover dialog did not close; refusing to continue with an unverified cover.",
            retryable=True,
        ) from exc


async def _set_schedule(page: Any, request: FillRequest) -> None:
    schedule = request.schedule_text()
    if not schedule:
        return
    radio = await require_visible(
        page,
        ("[class^='radio']:has-text('定时发布')", "label:has-text('定时发布')"),
        "schedule radio",
    )
    await guarded_click(radio, intent="select_schedule")
    date_input = await fill_input(
        page,
        ('.semi-input[placeholder="日期和时间"]', 'input[placeholder="日期和时间"]'),
        schedule,
        "schedule",
    )
    await close_date_picker(page)
    await page.wait_for_timeout(800)
    value = await date_input.input_value()
    if schedule not in value:
        raise FillError("field_verify_failed", f"Schedule did not persist: expected {schedule}.")


async def fill_douyin(page: Any, request: FillRequest, fields: list[str] | None = None) -> list[str]:
    if fields is None:
        fields = []
    await page.goto(UPLOAD_URL, wait_until="load", timeout=90000)
    await apace("task-switch")
    await check_account_state(page, "douyin")
    await _set_video(page, request)
    editor = await _description_editor(page, request.timeout_seconds())
    await fill_input(page, _TITLE_SELECTORS, request.title(), "title")
    fields.append("title")
    await apace("field-switch")
    await fill_rich_text(page, editor, request.desc(), "description")
    if request.desc():
        fields.append("description")
    if request.tags():
        await append_tags(page, editor, request.tags(), request.desc())
        fields.append("tags")
    await wait_for_upload(page, request.timeout_seconds(), platform="douyin")
    fields.insert(0, "video")
    await _set_cover(page, request)
    fields.append("cover")
    if request.schedule():
        await _set_schedule(page, request)
        fields.append("schedule")
    await check_account_state(page, "douyin")
    return fields
