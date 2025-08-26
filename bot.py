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

from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from telegram.constants import ParseMode

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
ADMIN_IDS: set[int] = set()

ROOT = Path(__file__).parent.resolve()
SNIPS = ROOT / 'snips'
META_FILE = SNIPS / 'meta.yaml'
MAX_MEDIA_SAVE_SIZE = 10 * 1024 * 1024
GIT_NAME: str | None = None
GIT_EMAIL: str | None = None
HASHTAG_RE = re.compile(r'#([A-Za-z0-9_-]+)')
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


def parse_markdown_media(md_text: str, base_dir: Path) -> Tuple[str, List[Path]]:
    media_paths: List[Path] = []
    def repl(m: re.Match) -> str:
        rel = m.group(1).strip()
        media_paths.append((base_dir / rel).resolve())
        return ''
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
        if not hashtags:
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
                continue
            plain_text, media_paths = parse_markdown_media(md, SNIPS)
            existing_media = [p for p in media_paths if p.exists()]
            if not existing_media:
                # send raw markdown text with formatting
                if plain_text:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=plain_text,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_to_message_id=reply_target,
                    )
                continue
            # prepare media group; if text is too long for a caption, send it separately
            caption = plain_text or ''
            long_caption = bool(caption and len(caption) > 1024)
            media_group = []
            for idx, p in enumerate(existing_media):
                with open(p, 'rb') as f:
                    bio = BytesIO(f.read())
                bio.name = p.name
                ext = p.suffix.lower()
                first_caption = idx == 0 and caption and not long_caption
                if ext in ('.jpg', '.jpeg', '.png', '.gif'):
                    media = InputMediaPhoto(media=bio, caption=caption if first_caption else None, parse_mode=ParseMode.MARKDOWN_V2)
                elif ext in ('.mp4', '.mov', '.mkv', '.webm'):
                    media = InputMediaVideo(media=bio, caption=caption if first_caption else None, parse_mode=ParseMode.MARKDOWN_V2)
                else:
                    media = InputMediaDocument(media=bio, caption=caption if first_caption else None, parse_mode=ParseMode.MARKDOWN_V2)
                media_group.append(media)
            # send text separately if caption was too long
            if long_caption:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    reply_to_message_id=reply_target,
                )
            if media_group:
                await context.bot.send_media_group(
                    chat_id=chat_id,
                    media=media_group,
                    reply_to_message_id=reply_target,
                )

    async def handle_save(update, context):
        m = update.effective_message
        c = update.effective_chat
        if c.type not in ('group', 'supergroup'):
            return
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
            await context.bot.send_message(chat_id=c.id, text='Usage: /save nameofhashtag')
            return
        hashtag = context.args[0]
        reply = m.reply_to_message
        # preserve original markdown-style formatting via message entities
        parts = []
        if reply.text:
            parts.append(reply.text_markdown_v2)
        if reply.caption:
            parts.append(reply.caption_markdown_v2)
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

        # normal save: write markdown and download media locally
        ensure_snips_dir()
        md_path = SNIPS / f"{hashtag}.md"
        # track saved files for git
        saved_files = [md_path]
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
                    prefix = '!' if reply.photo and idx == 0 else ''
                    lines.append(f"{prefix}[{ent.__class__.__name__}](./{save_path.name})")
                    saved_files.append(save_path)
            except Exception:
                continue

        # write out snippet markdown file
        with open(md_path, 'w', encoding='utf-8') as f:
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

        # commit new snip files to git
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

        await context.bot.send_message(chat_id=c.id, text=f"Saved snip '{hashtag}' (Markdown mode)")

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(CommandHandler('save', handle_save))
    app.run_polling()

if __name__ == '__main__':
    main()
