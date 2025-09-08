#!/usr/bin/env python3
import logging
import subprocess
import os
import re
import sys
import yaml
from io import BytesIO
from pathlib import Path
from typing import List, Tuple

from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG,
)
ADMIN_IDS: set[int] = set()

ROOT = Path(__file__).parent.resolve()
SNIPS = ROOT / 'snips'
META_FILE = SNIPS / 'meta.yaml'
MAX_MEDIA_SAVE_SIZE = 10 * 1024 * 1024
GIT_NAME: str | None = None
GIT_EMAIL: str | None = None
HASHTAG_RE = re.compile(r'#([\w-]+)', re.UNICODE)
MEDIA_RE = re.compile(r'!?\[[^\]]*\]\(([^)]+)\)')


def ensure_snips_dir():
    SNIPS.mkdir(parents=True, exist_ok=True)


def extract_hashtags(text: str) -> List[str]:
    return [tag.lower() for tag in HASHTAG_RE.findall(text or '')]


def load_snip_md(hashtag: str) -> str | None:
    p = SNIPS / f"{hashtag}.md"
    if not p.exists():
        return None
    with open(p, 'r', encoding='utf-8') as f:
        return f.read()

def load_snip_html(hashtag: str) -> str | None:
    p = SNIPS / f"{hashtag}.html"
    if not p.exists():
        return None
    with open(p, 'r', encoding='utf-8') as f:
        return f.read()

# dynamic spicy trigger words inferred from snippet files named 'spicy-<word>.html'
def load_spicy_triggers() -> List[str]:
    """Return list of spicy trigger words based on existing spicy-*.html snippets."""
    ensure_snips_dir()
    words: List[str] = []
    for p in SNIPS.glob('spicy-*.html'):
        stem = p.stem
        if '-' in stem:
            _, w = stem.split('-', 1)
            words.append(w.lower())
    return words

def extract_spicy_triggers(text: str, triggers: List[str]) -> List[str]:
    """Return spicy trigger words found in text, matching whole words case-insensitive."""
    found: List[str] = []
    text_lc = (text or '').lower()
    for t in triggers:
        if re.search(rf"\b{re.escape(t)}\b", text_lc):
            found.append(t)
    return found


def parse_markdown_media(md_text: str, base_dir: Path) -> Tuple[str, List[Path]]:
    media_paths: List[Path] = []
    def repl(m: re.Match) -> str:
        rel = m.group(1).strip()
        p = (base_dir / rel).resolve()
        if p.exists():
            media_paths.append(p)
            return ''
        return m.group(0)
    text = MEDIA_RE.sub(repl, md_text)
    return text, media_paths


def main():
    token = None
    cfg = ROOT / 'config.yaml'
    if cfg.exists():
        try:
            cfg_data = yaml.safe_load(cfg.read_text(encoding='utf-8')) or {}
            tg_conf = cfg_data.get('telegram') or {}
            token = tg_conf.get('token')
            # load admin whitelist (list of Telegram user IDs)
            ADMIN_IDS.update(int(a) for a in tg_conf.get('admins', []))
            git_conf = cfg_data.get('git') or {}
            global GIT_NAME, GIT_EMAIL
            GIT_NAME = git_conf.get('name')
            GIT_EMAIL = git_conf.get('email')
        except Exception:
            token = None
    if not token:
        token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        print(
            "Please set token via config.yaml (telegram.token) or TELEGRAM_BOT_TOKEN env var."
        )
        sys.exit(1)

    ensure_snips_dir()
    logging.info("Starting Telegram bot")

    app = ApplicationBuilder().token(token).build()

    async def handle_message(update, context):
        m = update.effective_message
        logging.debug("handle_message called: user=%s chat=%s text=%r", update.effective_user.id if update.effective_user else None, update.effective_chat.id if update.effective_chat else None, m.text)
        if not m or not m.text:
            return
        # always reply one level up: if this message was a reply, follow that chain, else reply to tag message
        if m.reply_to_message:
            reply_target = m.reply_to_message.message_id
        else:
            reply_target = m.message_id
        chat_id = update.effective_chat.id
        # load saved forward references (tag -> chat_id/message_id)
        try:
            meta = yaml.safe_load(META_FILE.read_text(encoding='utf-8')) or {}
        except Exception:
            meta = {}
        hashtags = extract_hashtags(m.text)
        spicy_words = load_spicy_triggers()
        spicy_matches = extract_spicy_triggers(m.text, spicy_words)
        logging.debug("extract_hashtags -> %s, spicy_triggers -> %s", hashtags, spicy_matches)
        # for spicy triggers, always reply to the actual triggering message
        spicy_target = m.message_id
        if not hashtags and not spicy_matches:
            return
        for tag in hashtags:
            # if we have a forward-reference for this snippet, just copy it (hides original sender)
            if tag in meta:
                ref = meta[tag]
                await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=ref['chat_id'],
                    message_id=ref['message_id'],
                    reply_to_message_id=reply_target,
                )
                continue
            md = load_snip_md(tag)
            if md is None:
                html = load_snip_html(tag)
                if html is None:
                    continue
                # try single voice note first
                files = [p for p in sorted(SNIPS.glob(f"{tag}_*")) if p.is_file()]
                if len(files) == 1 and files[0].suffix.lower() in ('.oga', '.ogg', '.opus'):
                    with open(files[0], 'rb') as vf:
                        await context.bot.send_voice(
                            chat_id=chat_id,
                            voice=vf,
                            caption=html,
                            parse_mode=ParseMode.HTML if html else None,
                            reply_to_message_id=spicy_target,
                        )
                    continue
                # bundle HTML snippet and any related media in one media group
                media_group: list = []
                for idx, p in enumerate(files):
                    with open(p, 'rb') as f:
                        bio = BytesIO(f.read())
                    bio.name = p.name
                    ext = p.suffix.lower()
                    first = idx == 0
                    if ext in ('.jpg', '.jpeg', '.png', '.gif'):
                        media = InputMediaPhoto(
                            media=bio,
                            caption=html if first else None,
                            parse_mode=ParseMode.HTML if first else None,
                        )
                    elif ext in ('.mp4', '.mov', '.mkv', '.webm'):
                        media = InputMediaVideo(
                            media=bio,
                            caption=html if first else None,
                            parse_mode=ParseMode.HTML if first else None,
                        )
                    elif ext in ('.oga', '.ogg', '.opus'):
                        media = InputMediaAudio(
                            media=bio,
                            caption=html if first else None,
                            parse_mode=ParseMode.HTML if first else None,
                        )
                    else:
                        media = InputMediaDocument(
                            media=bio,
                            caption=html if first else None,
                            parse_mode=ParseMode.HTML if first else None,
                        )
                    media_group.append(media)
                if media_group:
                    await context.bot.send_media_group(
                        chat_id=chat_id,
                        media=media_group,
                        reply_to_message_id=reply_target,
                    )
                else:
                    # no media; send HTML text alone
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=html,
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=reply_target,
                    )
                continue
            plain_text, media_paths = parse_markdown_media(md, SNIPS)
            existing_media = [p for p in media_paths if p.exists()]
            if not existing_media:
                # send raw markdown text with formatting
                if plain_text:
                    text_escaped = escape_markdown(plain_text, version=2)
                    for ch in '*_[]()':
                        text_escaped = text_escaped.replace(f'\\{ch}', ch)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=text_escaped,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_to_message_id=reply_target,
                    )
                continue
            # prepare media group; if text is too long for a caption, send it separately
            caption = plain_text or ''
            long_caption = bool(caption and len(caption) > 1024)
            caption_escaped = escape_markdown(caption, version=2) if caption else ''
            for ch in '*_[]()':
                caption_escaped = caption_escaped.replace(f'\\{ch}', ch)
            # single voice-note file? send as a true voice message
            if len(existing_media) == 1 and existing_media[0].suffix.lower() in ('.oga', '.ogg', '.opus'):
                with open(existing_media[0], 'rb') as vf:
                    await context.bot.send_voice(
                        chat_id=chat_id,
                        voice=vf,
                        caption=caption_escaped if not long_caption else None,
                        parse_mode=ParseMode.MARKDOWN_V2 if caption and not long_caption else None,
                        reply_to_message_id=reply_target,
                    )
                # if caption was too long, send it separately before or after
                if long_caption:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=caption_escaped,
                        reply_to_message_id=reply_target,
                    )
                continue
            media_group = []
            for idx, p in enumerate(existing_media):
                with open(p, 'rb') as f:
                    bio = BytesIO(f.read())
                bio.name = p.name
                ext = p.suffix.lower()
                first_caption = idx == 0 and caption and not long_caption
                if ext in ('.jpg', '.jpeg', '.png', '.gif'):
                    media = InputMediaPhoto(
                        media=bio,
                        caption=caption_escaped if first_caption else None,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                elif ext in ('.mp4', '.mov', '.mkv', '.webm'):
                    media = InputMediaVideo(
                        media=bio,
                        caption=caption_escaped if first_caption else None,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                elif ext in ('.oga', '.ogg', '.opus'):
                    media = InputMediaAudio(
                        media=bio,
                        caption=caption_escaped if first_caption else None,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                else:
                    media = InputMediaDocument(
                        media=bio,
                        caption=caption_escaped if first_caption else None,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                media_group.append(media)
            # send text separately if caption was too long
            if long_caption:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption_escaped,
                    reply_to_message_id=reply_target,
                )
            if media_group:
                await context.bot.send_media_group(
                    chat_id=chat_id,
                    media=media_group,
                    reply_to_message_id=reply_target,
                )

        # handle spicy trigger words (non-hashtag) serving snippets named 'spicy-<word>'
        for trig in spicy_matches:
            tag = f"spicy-{trig}"
            md = load_snip_md(tag)
            if md is None:
                html = load_snip_html(tag)
                if html is None:
                    continue
                # try single voice note first
                files = [p for p in sorted(SNIPS.glob(f"{tag}_*")) if p.is_file()]
                if len(files) == 1 and files[0].suffix.lower() in ('.oga', '.ogg', '.opus'):
                    with open(files[0], 'rb') as vf:
                        await context.bot.send_voice(
                            chat_id=chat_id,
                            voice=vf,
                            caption=html,
                            parse_mode=ParseMode.HTML if html else None,
                            reply_to_message_id=spicy_target,
                        )
                    continue
                # bundle HTML snippet and any related media in one media group
                media_group: list = []
                for idx, p in enumerate(files):
                    with open(p, 'rb') as f:
                        bio = BytesIO(f.read())
                    bio.name = p.name
                    ext = p.suffix.lower()
                    first = idx == 0
                    if ext in ('.jpg', '.jpeg', '.png', '.gif'):
                        media = InputMediaPhoto(
                            media=bio,
                            caption=html if first else None,
                            parse_mode=ParseMode.HTML if first else None,
                        )
                    elif ext in ('.mp4', '.mov', '.mkv', '.webm'):
                        media = InputMediaVideo(
                            media=bio,
                            caption=html if first else None,
                            parse_mode=ParseMode.HTML if first else None,
                        )
                    elif ext in ('.oga', '.ogg', '.opus'):
                        media = InputMediaAudio(
                            media=bio,
                            caption=html if first else None,
                            parse_mode=ParseMode.HTML if first else None,
                        )
                    else:
                        media = InputMediaDocument(
                            media=bio,
                            caption=html if first else None,
                            parse_mode=ParseMode.HTML if first else None,
                        )
                    media_group.append(media)
                if media_group:
                    await context.bot.send_media_group(
                        chat_id=chat_id,
                        media=media_group,
                        reply_to_message_id=spicy_target,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=html,
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=spicy_target,
                    )
                continue
            plain_text, media_paths = parse_markdown_media(md, SNIPS)
            existing_media = [p for p in media_paths if p.exists()]
            if not existing_media:
                if plain_text:
                    text_escaped = escape_markdown(plain_text, version=2)
                    for ch in '*_[]()':
                        text_escaped = text_escaped.replace(f'\\{ch}', ch)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=text_escaped,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_to_message_id=spicy_target,
                    )
                continue
            caption = plain_text or ''
            long_caption = bool(caption and len(caption) > 1024)
            caption_escaped = escape_markdown(caption, version=2) if caption else ''
            for ch in '*_[]()':
                caption_escaped = caption_escaped.replace(f'\\{ch}', ch)
            if len(existing_media) == 1 and existing_media[0].suffix.lower() in ('.oga', '.ogg', '.opus'):
                with open(existing_media[0], 'rb') as vf:
                    await context.bot.send_voice(
                        chat_id=chat_id,
                        voice=vf,
                        caption=caption_escaped if not long_caption else None,
                        parse_mode=ParseMode.MARKDOWN_V2 if caption and not long_caption else None,
                        reply_to_message_id=spicy_target,
                    )
                if long_caption:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=caption_escaped,
                        reply_to_message_id=spicy_target,
                    )
                continue
            media_group = []
            for idx, p in enumerate(existing_media):
                with open(p, 'rb') as f:
                    bio = BytesIO(f.read())
                bio.name = p.name
                ext = p.suffix.lower()
                first_caption = idx == 0 and caption and not long_caption
                if ext in ('.jpg', '.jpeg', '.png', '.gif'):
                    media = InputMediaPhoto(
                        media=bio,
                        caption=caption_escaped if first_caption else None,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                elif ext in ('.mp4', '.mov', '.mkv', '.webm'):
                    media = InputMediaVideo(
                        media=bio,
                        caption=caption_escaped if first_caption else None,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                elif ext in ('.oga', '.ogg', '.opus'):
                    media = InputMediaAudio(
                        media=bio,
                        caption=caption_escaped if first_caption else None,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                else:
                    media = InputMediaDocument(
                        media=bio,
                        caption=caption_escaped if first_caption else None,
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                media_group.append(media)
            if long_caption:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption_escaped,
                    reply_to_message_id=reply_target,
                )
            if media_group:
                await context.bot.send_media_group(
                    chat_id=chat_id,
                    media=media_group,
                    reply_to_message_id=spicy_target,
                )

    async def handle_save(update, context):
        m = update.effective_message
        c = update.effective_chat
        logging.debug("handle_save called: user=%s chat=%s args=%s reply_to=%s", update.effective_user.id if update.effective_user else None, c.id if c else None, context.args, m.reply_to_message.message_id if m and m.reply_to_message else None)
        if not m or not m.reply_to_message:
            return
        # only allow whitelisted user IDs to save snips
        if update.effective_user.id not in ADMIN_IDS:
            await context.bot.send_message(
                chat_id=c.id,
                text=f"ERROR: Permission denied ({update.effective_user.id})",
            )
            return
        if not context.args or len(context.args) < 1:
            await context.bot.send_message(
                chat_id=c.id,
                text='Usage: /saveng nameofhashtag (alias: /save)',
            )
            return
        hashtag = context.args[0].lower()
        reply = m.reply_to_message
        # preserve original markdown-style formatting via message entities
        parts = []
        if reply.text:
            parts.append(reply.text_html)
        if reply.caption:
            parts.append(reply.caption_html)
        # collect all media attachments (photo, document, video, audio, voice, animation, video_note)
        media_entries: list = []
        if reply.photo:
            media_entries.append(reply.photo[-1])
        for attr in ('document', 'video', 'audio', 'voice', 'animation', 'video_note'):
            ent = getattr(reply, attr, None)
            if ent:
                media_entries.append(ent)

        # if total media size is too large, record forward-only snippet
        total_media_size = sum((getattr(ent, 'file_size', 0) or 0) for ent in media_entries)
        if total_media_size > MAX_MEDIA_SAVE_SIZE:
            try:
                meta = yaml.safe_load(META_FILE.read_text(encoding='utf-8')) or {}
            except Exception:
                meta = {}
            meta[hashtag] = {
                'chat_id': reply.chat.id,
                'message_id': reply.message_id,
            }
            with open(META_FILE, 'w', encoding='utf-8') as mf:
                yaml.safe_dump(meta, mf)
            await context.bot.send_message(
                chat_id=c.id,
                text=f"Saved snippet '{hashtag}' (forward-only; media too large)",
            )
            return

        ensure_snips_dir()
        html_path = SNIPS / f"{hashtag}.html"
        saved_files = [html_path]
        lines = ['\n'.join(parts)] if parts else []
        for idx, ent in enumerate(media_entries):
            f1 = await context.bot.get_file(ent.file_id)
            if getattr(ent, 'file_name', None):
                fname = ent.file_name
            else:
                fname = Path(f1.file_path or ent.file_unique_id).name
            save_path = SNIPS / f"{hashtag}_{idx}{Path(fname).suffix}"
            try:
                await f1.download_to_drive(save_path)
                if save_path.exists():
                    saved_files.append(save_path)
            except Exception:
                continue

        with open(html_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines).strip() + '\n')

        # remove forward-only entry now that we have local snippet files
        if META_FILE.exists():
            try:
                meta = yaml.safe_load(META_FILE.read_text(encoding='utf-8')) or {}
            except Exception:
                meta = {}
            if hashtag in meta:
                del meta[hashtag]
                with open(META_FILE, 'w', encoding='utf-8') as mf:
                    yaml.safe_dump(meta, mf)

        try:
            # stage new snippet files and meta.yaml
            cmd = ['git', 'add'] + [str(p) for p in saved_files] + [str(META_FILE)]
            subprocess.run(cmd, check=True, cwd=str(SNIPS))
            # only commit & push if there are staged changes
            if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=str(SNIPS)).returncode != 0:
                user = update.effective_user.username or update.effective_user.first_name
                commit_msg = f"#{hashtag} added by @{user}"
                commit_cmd = ['git']
                if GIT_NAME:
                    commit_cmd += ['-c', f'user.name={GIT_NAME}']
                if GIT_EMAIL:
                    commit_cmd += ['-c', f'user.email={GIT_EMAIL}']
                commit_cmd += ['commit', '-m', commit_msg]
                subprocess.run(commit_cmd, check=True, cwd=str(SNIPS))
                subprocess.run(['git', 'push'], check=True, cwd=str(SNIPS))
        except Exception as e:
            logging.error(
                "Git commit/push failed (%s). If you see 'Author identity unknown', please add a [git] section to config.yaml with 'name' and 'email'.",
                e,
            )

        await context.bot.send_message(chat_id=c.id, text=f"Saved snip '{hashtag}' (HTML mode)")

    async def handle_savespicy(update, context):
        m = update.effective_message
        c = update.effective_chat
        logging.debug("handle_savespicy called: user=%s chat=%s args=%s reply_to=%s", update.effective_user.id if update.effective_user else None, c.id if c else None, context.args, m.reply_to_message.message_id if m and m.reply_to_message else None)
        if not m or not m.reply_to_message:
            return
        if update.effective_user.id not in ADMIN_IDS:
            await context.bot.send_message(chat_id=c.id, text=f"ERROR: Permission denied ({update.effective_user.id})")
            return
        if not context.args or len(context.args) < 1:
            await context.bot.send_message(chat_id=c.id, text='Usage: /savespicy triggerword')
            return
        trig = context.args[0].lower()
        reply = m.reply_to_message
        ensure_snips_dir()
        fullname = f"spicy-{trig}"
        html_path = SNIPS / f"{fullname}.html"
        saved_files: list[Path] = [html_path]
        parts: list[str] = []
        if reply.text:
            parts.append(reply.text_html)
        if reply.caption:
            parts.append(reply.caption_html)
        media_entries: list = []
        if reply.photo:
            media_entries.append(reply.photo[-1])
        for attr in ('document', 'video', 'audio', 'voice', 'animation', 'video_note'):
            ent = getattr(reply, attr, None)
            if ent:
                media_entries.append(ent)
        for idx, ent in enumerate(media_entries):
            f1 = await context.bot.get_file(ent.file_id)
            fname = getattr(ent, 'file_name', Path(f1.file_path or ent.file_unique_id).name)
            save_path = SNIPS / f"{fullname}_{idx}{Path(fname).suffix}"
            try:
                await f1.download_to_drive(save_path)
                if save_path.exists():
                    saved_files.append(save_path)
            except Exception:
                continue
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(parts).strip() + '\n')
        try:
            cmd = ['git', 'add'] + [str(p) for p in saved_files]
            subprocess.run(cmd, check=True, cwd=str(SNIPS))
            if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=str(SNIPS)).returncode != 0:
                user = update.effective_user.username or update.effective_user.first_name
                commit_cmd = ['git']
                if GIT_NAME:
                    commit_cmd += ['-c', f'user.name={GIT_NAME}']
                if GIT_EMAIL:
                    commit_cmd += ['-c', f'user.email={GIT_EMAIL}']
                commit_cmd += ['commit', '-m', f"#spicy-{trig} added by @{user}"]
                subprocess.run(commit_cmd, check=True, cwd=str(SNIPS))
                subprocess.run(['git', 'push'], check=True, cwd=str(SNIPS))
        except Exception as e:
            logging.error("Git commit/push failed for spicy snippet (%s)", e)

        await context.bot.send_message(chat_id=c.id, text=f"Saved spicy snip '{trig}' (HTML mode)")

    async def handle_listng(update, context):
        ensure_snips_dir()
        md_tags = {p.stem for p in SNIPS.glob("*.md")}
        html_tags = {p.stem for p in SNIPS.glob("*.html")}
        media_tags: set[str] = set()
        for p in SNIPS.iterdir():
            if p.is_file() and p.suffix.lower() not in ('.md', '.html') and '_' in p.stem:
                media_tags.add(p.stem.split('_', 1)[0])
        tags = sorted(md_tags | html_tags | media_tags)
        chat_id = update.effective_chat.id
        if not tags:
            await context.bot.send_message(chat_id=chat_id, text="No snippets available.")
            return
        parts = []
        chunk = ""
        for tag in tags:
            part = f"#{tag}\n"
            if len(chunk) + len(part) > 4000:
                parts.append(chunk)
                chunk = ""
            chunk += part
        if chunk:
            parts.append(chunk)
        for chunk in parts:
            await context.bot.send_message(chat_id=chat_id, text=chunk)

    async def error_handler(update, context):
        logging.error("Exception while handling update", exc_info=context.error)
        if update and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"⚠️ An error occurred:\n{context.error}",
                )
            except Exception as exc:
                logging.error("Failed to send error message to user", exc_info=exc)

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    # handle both /saveng and legacy /save aliases
    app.add_handler(CommandHandler(['saveng', 'save'], handle_save))
    app.add_handler(CommandHandler('listng', handle_listng))
    app.add_handler(CommandHandler('savespicy', handle_savespicy))
    app.add_error_handler(error_handler)
    app.run_polling()

if __name__ == '__main__':
    main()
