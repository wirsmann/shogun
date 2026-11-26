#!/usr/bin/env python3
"""Apply Channel Studio overrides to a freshly generated M3U playlist."""

import argparse
import base64
import codecs
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path


IDENTITY_FIELDS = ("tvg_id", "tvg_name", "group_title", "title", "epg_source")
SET_FIELDS = {"name", "tvg_id", "epg_source"}


class ConfigError(ValueError):
    """Raised when an edit config cannot safely be applied."""


def stable_channel_id(match):
    identity = [match[field] for field in IDENTITY_FIELDS]
    identity.append(match["occurrence"])
    serialized = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(serialized.encode("utf-8")).decode("ascii").rstrip("=")


def split_extinf(line):
    quoted = False
    for index, character in enumerate(line):
        if character == '"':
            quoted = not quoted
        elif character == "," and not quoted:
            return line[:index], line[index + 1:]
    raise ConfigError("Found an #EXTINF line without a channel-name separator.")


def parse_attributes(metadata):
    attributes = {}
    for match in re.finditer(r'(?:^|\s)([\w-]+)="([^"]*)"', metadata):
        attributes[match.group(1).lower()] = match.group(2)
    return attributes


def line_content_and_ending(line):
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1:]
    return line, ""


def playlist_identities(lines):
    occurrences = defaultdict(int)
    identities = {}
    for line_index, line in enumerate(lines):
        content, _ending = line_content_and_ending(line)
        if not content.startswith("#EXTINF:"):
            continue
        metadata, title = split_extinf(content)
        attributes = parse_attributes(metadata)
        identity = (
            attributes.get("tvg-id", ""),
            attributes.get("tvg-name", ""),
            attributes.get("group-title", ""),
            title.strip(),
            attributes.get("epg-source", ""),
        )
        occurrence = occurrences[identity]
        occurrences[identity] += 1
        identities[identity + (occurrence,)] = line_index
    return identities


def channel_stream_line(lines, start_index):
    for line_index in range(start_index + 1, len(lines)):
        content, _ending = line_content_and_ending(lines[line_index])
        if content.startswith("#EXTINF:"):
            break
        if not content.strip() or content.startswith("#"):
            continue
        return line_index
    raise ConfigError(f"Channel at line {start_index + 1} has no stream URL.")


def validate_config(config, playlist_path):
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
        raise ConfigError("Config must be a version 1 JSON object.")
    configured_playlist = config.get("playlist")
    if not isinstance(configured_playlist, str) or (
        configured_playlist != "*" and Path(configured_playlist).name != configured_playlist
    ):
        raise ConfigError("Config playlist must be a file name, not a path.")
    if configured_playlist != "*" and configured_playlist.casefold() != playlist_path.name.casefold():
        raise ConfigError(
            f"Config targets {configured_playlist!r}, not {playlist_path.name!r}."
        )
    entries = config.get("channels")
    if not isinstance(entries, list):
        raise ConfigError("Config 'channels' must be a list.")

    validated = []
    seen = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"Channel edit {index + 1} must be an object.")
        match = entry.get("match")
        changes = entry.get("set")
        if not isinstance(match, dict) or not isinstance(changes, dict):
            raise ConfigError(f"Channel edit {index + 1} needs 'match' and 'set' objects.")
        remove = entry.get("remove", False)
        if not isinstance(remove, bool):
            raise ConfigError(f"Channel edit {index + 1} has an invalid removal flag.")
        if any(not isinstance(match.get(field), str) for field in IDENTITY_FIELDS):
            raise ConfigError(f"Channel edit {index + 1} has an invalid match identity.")
        occurrence = match.get("occurrence")
        if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0:
            raise ConfigError(f"Channel edit {index + 1} has an invalid occurrence.")
        if not isinstance(match.get("epg_source"), str):
            raise ConfigError(f"Channel edit {index + 1} has an invalid EPG source identity.")
        channel_id = entry.get("id")
        if not isinstance(channel_id, str) or stable_channel_id(match) != channel_id:
            raise ConfigError(f"Channel edit {index + 1} has an invalid stable channel ID.")
        unknown_fields = set(changes) - SET_FIELDS
        if unknown_fields:
            raise ConfigError(
                f"Channel edit {index + 1} has unsupported fields: "
                + ", ".join(sorted(unknown_fields))
            )
        if (not changes and not remove) or any(not isinstance(value, str) for value in changes.values()):
            raise ConfigError(f"Channel edit {index + 1} must contain string changes or remove the channel.")
        identity = tuple(match[field] for field in IDENTITY_FIELDS) + (occurrence,)
        if identity in seen:
            raise ConfigError(f"Config contains a duplicate edit for channel {index + 1}.")
        seen.add(identity)
        validated.append((identity, changes, remove))
    return validated


def set_attribute(line, name, value):
    metadata, title = split_extinf(line)
    escaped = value.replace('"', "&quot;")
    pattern = re.compile(r'(^|\s)' + re.escape(name) + r'="[^"]*"', re.IGNORECASE)
    if pattern.search(metadata):
        metadata = pattern.sub(lambda match: match.group(1) + name + '="' + escaped + '"', metadata, count=1)
    else:
        metadata = metadata.rstrip() + " " + name + '="' + escaped + '"'
    return metadata + "," + title


def remove_attribute(line, name):
    metadata, title = split_extinf(line)
    pattern = re.compile(r'\s+' + re.escape(name) + r'="[^"]*"', re.IGNORECASE)
    return pattern.sub("", metadata, count=1) + "," + title


def apply_changes(line, changes):
    content, ending = line_content_and_ending(line)
    if "name" in changes:
        content = set_attribute(content, "tvg-name", changes["name"])
        metadata, _old_title = split_extinf(content)
        content = metadata + "," + changes["name"]
    if "tvg_id" in changes:
        content = set_attribute(content, "tvg-id", changes["tvg_id"])
    if "epg_source" in changes:
        if changes["epg_source"] == "epgstrong":
            content = remove_attribute(content, "epg-source")
        else:
            content = set_attribute(content, "epg-source", changes["epg_source"])
    return content + ending


def apply_config(playlist_path, config_path):
    playlist_path = Path(playlist_path)
    config_path = Path(config_path)
    config_text = config_path.read_text(encoding="utf-8-sig")
    config = json.loads(config_text)
    edits = validate_config(config, playlist_path)

    original = playlist_path.read_bytes()
    has_bom = original.startswith(codecs.BOM_UTF8)
    text = original.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    if not any(line_content_and_ending(line)[0].startswith("#EXTINF:") for line in lines):
        raise ConfigError(f"No #EXTINF channels found in {playlist_path}.")
    identities = playlist_identities(lines)

    missing = [identity for identity, _changes, _remove in edits if identity not in identities]
    if missing and config["playlist"] != "*":
        example = missing[0]
        raise ConfigError(
            f"{len(missing)} configured channel(s) no longer match {playlist_path}; "
            f"first missing identity is {example[:5]!r} (occurrence {example[5]})."
        )
    applicable = [
        (entry, edit)
        for entry, edit in zip(config["channels"], edits)
        if edit[0] in identities
    ]
    if not applicable:
        return 0, len(missing)

    removals = []
    for edit, (identity, changes, remove) in applicable:
        line_index = identities[identity]
        if remove:
            removals.append((line_index, channel_stream_line(lines, line_index)))
            continue
        updated_line = apply_changes(lines[line_index], changes)
        content, ending = line_content_and_ending(updated_line)
        lines[line_index] = set_attribute(content, "channel-studio-id", edit["id"]) + ending

    for start_index, end_index in sorted(removals, reverse=True):
        del lines[start_index:end_index + 1]

    output = ("".join(lines)).encode("utf-8")
    if has_bom:
        output = codecs.BOM_UTF8 + output
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=playlist_path.name + ".", suffix=".tmp",
            dir=str(playlist_path.parent), delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(output)
        os.replace(str(temp_path), str(playlist_path))
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return len(applicable), len(missing)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playlist", default="wes.m3u", help="Generated playlist to update")
    parser.add_argument("--config", default="edits.json", help="Channel Studio JSON config")
    args = parser.parse_args()
    try:
        count, skipped = apply_config(args.playlist, args.config)
    except (OSError, UnicodeError, json.JSONDecodeError, ConfigError) as error:
        parser.exit(1, f"Could not apply playlist config: {error}\n")
    print(f"Applied {count} channel config record(s) from {args.config} to {args.playlist}.")
    if skipped:
        print(f"Warning: skipped {skipped} config record(s) not found in {args.playlist}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
