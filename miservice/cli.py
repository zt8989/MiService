import asyncio
import json
import logging
import os
import random
import sys
import tempfile
from pathlib import Path

import click
from aiohttp import ClientSession
from mutagen.mp3 import MP3
from rich import print

from miservice import (
    MiAccount,
    MiNAService,
    MiIOService,
    miio_command,
    miio_command_help,
)

_LOGGER = logging.getLogger("miservice")

device_id_list = []
SPECIAL_MINA_COMMANDS = {
    "message",
    "play",
    "mina",
    "pause",
    "stop",
    "loop",
    "play_list",
    "suno",
    "suno_random",
}


def usage():
    print("MiService - XiaoMi Cloud Service\n")
    print("Usage: The following variables must be set:")
    print("           export MI_USER=<Username>")
    print("           export MI_PASS=<Password>")
    print("           export MI_DID=<Device ID|Name>\n")
    print(miio_command_help(prefix="micli" + " "))


def find_device_id(hardware_data, mi_did):
    for h in hardware_data:
        if h.get("miotDID", "") == str(mi_did):
            return h.get("deviceID")
    raise click.ClickException("we have no mi_did: please use `micli mina` to check")


async def _get_duration(url: str, start=0, end=500):
    url = url.strip()
    url_base = url.split("?")[0]
    if not url_base.endswith(".mp3"):
        async with ClientSession() as session:
            async with session.get(
                url,
                allow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/39.0.2171.95 Safari/537.36"
                },
            ) as response:
                url = response.url

    headers = {"Range": f"bytes={start}-{end}"}
    async with ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            array_buffer = await response.read()
    with tempfile.NamedTemporaryFile() as tmp:
        tmp.write(array_buffer)
        try:
            m = MP3(tmp)
        except Exception:  # pylint: disable=broad-except
            headers = {"Range": f"bytes={0}-{1000}"}
            async with ClientSession() as retry_session:
                async with retry_session.get(url, headers=headers) as response:
                    array_buffer = await response.read()
        with tempfile.NamedTemporaryFile() as tmp2:
            tmp2.write(array_buffer)
            m = MP3(tmp2)
    return m.info.length


async def get_suno_playlist(is_random=False):
    suno_playlist_url = "https://studio-api.suno.ai/api/playlist/1190bf92-10dc-4ce5-968a-7a377f37f984/?page=1"
    song_play_dict = {}
    async with ClientSession() as session:
        async with session.get(suno_playlist_url) as response:
            data = await response.json()
            for d in data["playlist_clips"]:
                clip = d.get("clip")
                if clip and clip.get("audio_url"):
                    song_play_dict[clip["audio_url"]] = clip.get("title")
    return song_play_dict


async def miservice_stop(device_id):
    async with ClientSession() as session:
        env = os.environ
        account = MiAccount(
            session,
            env.get("MI_USER"),
            env.get("MI_PASS"),
            os.path.join(str(Path.home()), ".mi.token"),
        )
        mina_service = MiNAService(account)
        await mina_service.player_stop(device_id)
    print("Stop")


async def _handle_mina_command(account, env, args):
    action = args[0]
    mina_service = MiNAService(account)
    if not env.get("MI_DID"):
        raise click.ClickException("Please export MI_DID in your env")
    result = await mina_service.device_list()
    device_id = find_device_id(result, env.get("MI_DID"))
    device_id_list.append(device_id)

    if len(args) == 1:
        if action in {"pause", "stop"}:
            await mina_service.player_stop(device_id)
        elif action == "mina" and result:
            print(result[0])
        elif action in {"suno", "suno_random"}:
            song_dict = await get_suno_playlist()
            print(song_dict)
            song_urls = list(song_dict.keys())
            if action == "suno_random":
                random.shuffle(song_urls)
                print("Will play suno trending list randomly")
            else:
                print("Will play suno trending list")
            for song_url in song_urls:
                title = song_dict.get(song_url)
                print(f"Will play {song_url.strip()} title {title}")
                duration = await _get_duration(song_url)
                await mina_service.play_by_url(device_id, song_url.strip())
                await asyncio.sleep(duration)
            await mina_service.player_stop(device_id)
        else:
            print("Please provide a play URL")
        return ""

    if action == "loop":
        if len(args) < 2:
            raise click.ClickException("Usage: micli loop <url>")
        url = args[1]
        await mina_service.play_by_url(device_id, url)
        await mina_service.player_set_loop(device_id, 0)
        return ""
    if action == "play":
        if len(args) < 2:
            raise click.ClickException("Usage: micli play <url>")
        url = args[1]
        await mina_service.play_by_url(device_id, url)
        await mina_service.player_set_loop(device_id, 1)
        return ""
    if action == "play_list":
        if len(args) < 2:
            raise click.ClickException("Usage: micli play_list <file>")
        await mina_service.player_set_loop(device_id, 1)
        file_name = args[1]
        try:
            with open(file_name, encoding="utf8") as f:
                lines = [line.strip() for line in f if line.strip()]
        except Exception as exc:  # pylint: disable=broad-except
            print(exc)
            return ""
        for line in lines:
            print(f"Will play {line}")
            duration = await _get_duration(line)
            await mina_service.play_by_url(device_id, line)
            await asyncio.sleep(duration)
        await mina_service.player_stop(device_id)
        return ""
    if action == "message":
        if len(args) < 2:
            raise click.ClickException("Usage: micli message <text>")
        text = args[1]
        await mina_service.text_to_speech(device_id, text)
        return ""

    return ""


async def _run_command(command_args):
    env = os.environ
    async with ClientSession() as session:
        account = MiAccount(
            session,
            env.get("MI_USER"),
            env.get("MI_PASS"),
            os.path.join(str(Path.home()), ".mi.token"),
        )
        if not command_args:
            raise click.ClickException("No command provided; see --help for usage")
        action = command_args[0]
        if action in SPECIAL_MINA_COMMANDS:
            result = await _handle_mina_command(account, env, command_args)
        else:
            service = MiIOService(account)
            args_line = " ".join(command_args)
            result = await miio_command(
                service, env.get("MI_DID"), args_line, sys.argv[0] + " "
            )
        if not isinstance(result, str):
            result = json.dumps(result, indent=2, ensure_ascii=False)
        return result


@click.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option("-v", "verbosity", count=True, help="Increase verbosity (-v for INFO, -vv for DEBUG)")
@click.option(
    "--log-level",
    type=click.Choice(
        ["NOTSET", "FATAL", "ERROR", "WARN", "INFO", "DEBUG"], case_sensitive=False
    ),
    default=None,
    help="Explicit logging level",
)
@click.argument("command_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def micli(ctx, verbosity, log_level, command_args):
    level = logging.WARNING
    if log_level:
        level = getattr(logging, log_level.upper(), logging.WARNING)
    elif verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    _LOGGER.setLevel(level)
    if not _LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        _LOGGER.addHandler(handler)
    else:
        for handler in _LOGGER.handlers:
            handler.setLevel(level)

    args = tuple(command_args) or tuple(ctx.args)
    if not args:
        usage()
        ctx.exit(0)

    try:
        result = asyncio.run(_run_command(args))
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        if device_id_list:
            asyncio.run(miservice_stop(device_id_list[0]))
        print(str(exc))
        ctx.exit(1)
    except Exception as exc:  # pylint: disable=broad-except
        print(str(exc))
        ctx.exit(1)
    else:
        if result is None:
            return
        print(result)
