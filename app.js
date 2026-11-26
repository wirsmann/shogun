(() => {
  "use strict";

  const PAGE_SIZE = 80;
  const STORAGE_KEY = "shogun-channel-studio:v1";
  const GUIDE_WINDOW_MS = 48 * 60 * 60 * 1000;
  const CHANNEL_COLLATOR = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
  const TV_BASE_URL = window.location.hostname === "tv.shogun.ca"
    ? window.location.origin
    : "http://tv.shogun.ca";
  const PLAYLIST_URL = `${TV_BASE_URL}/strong.m3u`;
  const EPGSTRONG_URL = `${TV_BASE_URL}/EPGStrong.xml`;
  const EDITS_URL = `${TV_BASE_URL}/edits.json`;
  const REMOTE_SEGMENTS = [
    ...Array.from({ length: 55 }, (_unused, index) => `temp${index + 1}.xml`),
    "temp900.xml"
  ];
  const state = {
    channels: [],
    playlistLines: [],
    playlistName: "strong.m3u",
    configuration: { version: 1, playlist: "*", channels: [] },
    sources: new Map(),
    selectedKey: null,
    query: "",
    group: "",
    unmatchedOnly: false,
    showRemoved: false,
    page: 0,
    overrides: {}
  };

  const ui = {
    appStatus: document.getElementById("app-status"),
    channelCount: document.getElementById("channel-count"),
    mappedCount: document.getElementById("mapped-count"),
    guideCount: document.getElementById("guide-count"),
    sourceSummary: document.getElementById("source-summary"),
    playlistFile: document.getElementById("playlist-file"),
    guideFiles: document.getElementById("guide-files"),
    exportConfig: document.getElementById("export-config"),
    exportPlaylist: document.getElementById("export-playlist"),
    epgstrongUrl: document.getElementById("epgstrong-url"),
    loadEpgstrong: document.getElementById("load-epgstrong"),
    remoteSegment: document.getElementById("remote-segment"),
    loadRemoteSegment: document.getElementById("load-remote-segment"),
    channelSearch: document.getElementById("channel-search"),
    groupFilter: document.getElementById("group-filter"),
    unmatchedFilter: document.getElementById("unmatched-filter"),
    removedFilter: document.getElementById("removed-filter"),
    channelList: document.getElementById("channel-list"),
    visibleCount: document.getElementById("visible-count"),
    pageLabel: document.getElementById("page-label"),
    previousPage: document.getElementById("previous-page"),
    nextPage: document.getElementById("next-page"),
    channelEditor: document.getElementById("channel-editor"),
    nowPlaying: document.getElementById("now-playing"),
    guideClock: document.getElementById("guide-clock")
  };

  function setStatus(message, isError) {
    ui.appStatus.textContent = message;
    ui.appStatus.classList.toggle("status-error", Boolean(isError));
  }

  function hashText(value) {
    let hash = 2166136261;
    for (let i = 0; i < value.length; i += 1) {
      hash ^= value.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(36);
  }

  function splitExtinf(line) {
    let quoted = false;
    for (let i = 0; i < line.length; i += 1) {
      if (line[i] === '"') quoted = !quoted;
      if (line[i] === "," && !quoted) {
        return { metadata: line.slice(0, i), name: line.slice(i + 1).trim() };
      }
    }
    return { metadata: line, name: "" };
  }

  function parseAttributes(metadata) {
    const attributes = {};
    const pattern = /(?:^|\s)([\w-]+)="([^"]*)"/g;
    let match;
    while ((match = pattern.exec(metadata)) !== null) {
      attributes[match[1].toLowerCase()] = match[2];
    }
    return attributes;
  }

  function identityId(identity) {
    const payload = JSON.stringify([
      identity.tvg_id,
      identity.tvg_name,
      identity.group_title,
      identity.title,
      identity.epg_source,
      identity.occurrence
    ]);
    const bytes = new TextEncoder().encode(payload);
    let binary = "";
    bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function identityFromId(id) {
    const base64 = id.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(base64 + "=".repeat((4 - base64.length % 4) % 4));
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    const values = JSON.parse(new TextDecoder().decode(bytes));
    if (!Array.isArray(values) || values.length !== 6 ||
        values.slice(0, 5).some((value) => typeof value !== "string") ||
        !Number.isInteger(values[5]) || values[5] < 0) {
      throw new Error("A channel-studio-id marker is invalid.");
    }
    const identity = {
      tvg_id: values[0],
      tvg_name: values[1],
      group_title: values[2],
      title: values[3],
      epg_source: values[4],
      occurrence: values[5]
    };
    if (identityId(identity) !== id) throw new Error("A channel-studio-id marker is not canonical.");
    return identity;
  }

  function restoreMarkerIdentities() {
    state.channels.forEach((channel) => {
      if (!channel.studioId) return;
      const identity = identityFromId(channel.studioId);
      channel.identity = identity;
      channel.configurationId = channel.studioId;
      channel.originalName = identity.tvg_name || identity.title;
      channel.originalEpgId = identity.tvg_id;
      channel.originalSourceId = identity.epg_source || "epgstrong";
    });
  }

  function parsePlaylist(text) {
    const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/);
    const channels = [];
    const identityCounts = new Map();
    lines.forEach((line, lineIndex) => {
      if (!line.startsWith("#EXTINF:")) return;
      const parts = splitExtinf(line);
      const attrs = parseAttributes(parts.metadata);
      const identity = {
        tvg_id: attrs["tvg-id"] || "",
        tvg_name: attrs["tvg-name"] || "",
        group_title: attrs["group-title"] || "",
        title: parts.name,
        epg_source: attrs["epg-source"] || ""
      };
      const identityKey = JSON.stringify([
        identity.tvg_id, identity.tvg_name, identity.group_title,
        identity.title, identity.epg_source
      ]);
      identity.occurrence = identityCounts.get(identityKey) || 0;
      identityCounts.set(identityKey, identity.occurrence + 1);
      let streamUrl = "";
      let endLineIndex = lineIndex;
      for (let i = lineIndex + 1; i < lines.length; i += 1) {
        if (lines[i].startsWith("#EXTINF:")) break;
        if (lines[i].trim() && !lines[i].startsWith("#")) {
          streamUrl = lines[i].trim();
          endLineIndex = i;
          break;
        }
      }
      const group = attrs["group-title"] || "Ungrouped";
      const name = attrs["tvg-name"] || parts.name || "Untitled channel";
      const keySeed = `${lineIndex}\u001f${attrs["tvg-id"] || ""}\u001f${name}\u001f${streamUrl}`;
      channels.push({
        key: hashText(keySeed),
        lineIndex,
        endLineIndex,
        identity,
        studioId: attrs["channel-studio-id"] || "",
        name,
        originalName: name,
        epgId: attrs["tvg-id"] || "",
        originalEpgId: attrs["tvg-id"] || "",
        sourceId: attrs["epg-source"] || "epgstrong",
        originalSourceId: attrs["epg-source"] || "epgstrong",
        group,
        logo: attrs["tvg-logo"] || "",
        streamUrl,
        removed: false,
        originalRemoved: false
      });
    });
    return { lines, channels };
  }

  function applyExistingConfiguration(configuration) {
    if (!configuration || configuration.version !== 1 ||
        (configuration.playlist !== "*" && configuration.playlist !== state.playlistName) ||
        !Array.isArray(configuration.channels)) {
      throw new Error("edits.json must be a version 1 config for this playlist or '*'.");
    }
    const records = new Map();
    configuration.channels.forEach((record) => {
      if (!record || typeof record.id !== "string" || !record.match ||
          !record.set || typeof record.set !== "object" || Array.isArray(record.set) ||
          (Object.keys(record.set).length === 0 && record.remove !== true) ||
          (Object.prototype.hasOwnProperty.call(record, "remove") && typeof record.remove !== "boolean") ||
          ["tvg_id", "tvg_name", "group_title", "title", "epg_source"]
            .some((field) => typeof record.match[field] !== "string") ||
          !Number.isInteger(record.match.occurrence) || record.match.occurrence < 0 ||
          identityId(record.match) !== record.id) {
        throw new Error("edits.json contains an invalid channel identity.");
      }
      if (Object.keys(record.set).some((field) =>
        !["name", "tvg_id", "epg_source"].includes(field) ||
        typeof record.set[field] !== "string"
      )) {
        throw new Error("edits.json contains an invalid channel edit.");
      }
      if (Object.prototype.hasOwnProperty.call(record, "remove") && typeof record.remove !== "boolean") {
        throw new Error("edits.json contains an invalid channel removal flag.");
      }
      if (records.has(record.id)) throw new Error("edits.json contains duplicate channel edits.");
      records.set(record.id, record);
    });
    state.configuration = configuration;
    restoreMarkerIdentities();

    state.channels.forEach((channel) => {
      const id = channel.studioId || identityId(channel.identity);
      const record = records.get(id);
      if (!record) return;
      channel.identity = record.match;
      channel.configurationId = record.id;
      channel.originalName = record.match.tvg_name || record.match.title;
      channel.originalEpgId = record.match.tvg_id;
      channel.originalSourceId = record.match.epg_source || "epgstrong";
      channel.name = Object.prototype.hasOwnProperty.call(record.set, "name")
        ? record.set.name
        : channel.originalName;
      channel.epgId = Object.prototype.hasOwnProperty.call(record.set, "tvg_id")
        ? record.set.tvg_id
        : channel.originalEpgId;
      channel.sourceId = Object.prototype.hasOwnProperty.call(record.set, "epg_source")
        ? record.set.epg_source
        : channel.originalSourceId;
      channel.removed = record.remove === true;
      channel.originalRemoved = channel.removed;
    });
  }

  function parseXmltvDate(value) {
    const match = String(value || "").match(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:\s*([+-])(\d{2})(\d{2}))?/);
    if (!match) return NaN;
    const localAsUtc = Date.UTC(
      Number(match[1]), Number(match[2]) - 1, Number(match[3]),
      Number(match[4]), Number(match[5]), Number(match[6])
    );
    if (!match[7]) return localAsUtc;
    const offsetMinutes = (Number(match[8]) * 60 + Number(match[9])) * (match[7] === "+" ? 1 : -1);
    return localAsUtc - offsetMinutes * 60 * 1000;
  }

  function hasParserError(document) {
    return document.getElementsByTagName("parsererror").length > 0;
  }

  function parseGuideDocument(text) {
    const parser = new DOMParser();
    const clean = text.replace(/^\uFEFF/, "").trim();
    let document = parser.parseFromString(clean, "application/xml");
    if (hasParserError(document)) {
      const fragments = clean.replace(/^\s*<\?xml[^?]*\?>/i, "");
      document = parser.parseFromString(`<tv>${fragments}</tv>`, "application/xml");
    }
    if (hasParserError(document) || !document.documentElement) {
      throw new Error("The guide is not valid XMLTV or an XMLTV segment.");
    }
    return document;
  }

  function addGuide(text, id, name, refreshUi) {
    const document = parseGuideDocument(text);
    const channels = new Map();
    Array.from(document.getElementsByTagName("channel")).forEach((node) => {
      const channelId = node.getAttribute("id");
      if (!channelId) return;
      const displayName = node.getElementsByTagName("display-name")[0];
      channels.set(channelId, displayName ? displayName.textContent.trim() : channelId);
    });

    const now = Date.now();
    const cutoff = now + GUIDE_WINDOW_MS;
    const programmes = new Map();
    Array.from(document.getElementsByTagName("programme")).forEach((node) => {
      const channelId = node.getAttribute("channel");
      const start = parseXmltvDate(node.getAttribute("start"));
      const stop = parseXmltvDate(node.getAttribute("stop"));
      if (!channelId || !Number.isFinite(start) || !Number.isFinite(stop) || stop <= now || start >= cutoff) return;
      const titleNode = node.getElementsByTagName("title")[0];
      const descNode = node.getElementsByTagName("desc")[0];
      const categoryNode = node.getElementsByTagName("category")[0];
      const item = {
        start,
        stop,
        title: titleNode ? titleNode.textContent.trim() : "Untitled programme",
        description: descNode ? descNode.textContent.trim() : "",
        category: categoryNode ? categoryNode.textContent.trim() : ""
      };
      if (!programmes.has(channelId)) programmes.set(channelId, []);
      programmes.get(channelId).push(item);
    });
    programmes.forEach((items) => items.sort((a, b) => a.start - b.start));

    state.sources.set(id, { id, name, channels, programmes });
    if (refreshUi !== false) {
      updateSourceUi();
      renderList();
      renderEditor();
      updateStats();
    }
  }

  function getCurrentChannel() {
    return state.channels.find((channel) => channel.key === state.selectedKey) || null;
  }

  function sortedSources() {
    return Array.from(state.sources.values())
      .sort((a, b) => CHANNEL_COLLATOR.compare(a.name, b.name));
  }

  function getProgram(channel, now) {
    if (!channel || !channel.epgId) return null;
    const source = state.sources.get(channel.sourceId);
    if (!source) return null;
    const schedule = source.programmes.get(channel.epgId) || [];
    const current = schedule.find((item) => item.start <= now && now < item.stop);
    if (current) return { item: current, isNext: false };
    const next = schedule.find((item) => item.start > now);
    return next ? { item: next, isNext: true } : null;
  }

  function hasGuideMatch(channel) {
    const source = state.sources.get(channel.sourceId);
    return Boolean(channel.epgId && source && source.channels.has(channel.epgId));
  }

  function persistEdits() {
    state.overrides = {};
    state.channels.forEach((channel) => {
      if (channel.name !== channel.originalName ||
          channel.epgId !== channel.originalEpgId ||
          channel.sourceId !== channel.originalSourceId ||
          channel.removed !== channel.originalRemoved) {
        state.overrides[channel.key] = {
          name: channel.name,
          epgId: channel.epgId,
          sourceId: channel.sourceId,
          removed: channel.removed
        };
      }
    });
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state.overrides));
      setStatus("Changes saved in this browser", false);
    } catch (error) {
      setStatus(`Could not save edits locally: ${error.message}`, true);
    }
  }

  function restoreEdits() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      state.overrides = saved ? JSON.parse(saved) : {};
      if (!state.overrides || Array.isArray(state.overrides) || typeof state.overrides !== "object") {
        throw new Error("Saved channel edits have an invalid format.");
      }
      state.channels.forEach((channel) => {
        const override = state.overrides[channel.key];
        if (!override || typeof override !== "object") return;
        if (typeof override.name === "string") channel.name = override.name;
        if (typeof override.epgId === "string") channel.epgId = override.epgId;
        if (typeof override.sourceId === "string") channel.sourceId = override.sourceId;
        if (typeof override.removed === "boolean") channel.removed = override.removed;
      });
    } catch (error) {
      state.overrides = {};
      setStatus(`Could not restore saved edits: ${error.message}`, true);
    }
  }

  function updateSourceUi() {
    ui.guideCount.textContent = String(state.sources.size);
    const names = sortedSources().map((source) => source.name);
    ui.sourceSummary.textContent = names.length
      ? `${names.length} guide${names.length === 1 ? "" : "s"} ready · ${names.join(" · ")}`
      : "No guide sources loaded yet";
  }

  function updateStats() {
    const activeChannels = state.channels.filter((channel) => !channel.removed);
    ui.channelCount.textContent = activeChannels.length.toLocaleString();
    ui.mappedCount.textContent = activeChannels.filter(hasGuideMatch).length.toLocaleString();
    ui.guideCount.textContent = String(state.sources.size);
  }

  function uniqueGroups() {
    return Array.from(new Set(state.channels.map((channel) => channel.group)))
      .sort((a, b) => CHANNEL_COLLATOR.compare(a, b));
  }

  function updateGroupFilter() {
    const selected = state.group;
    ui.groupFilter.replaceChildren(new Option("All groups", ""));
    uniqueGroups().forEach((group) => ui.groupFilter.add(new Option(group, group)));
    ui.groupFilter.value = selected;
  }

  function filteredChannels() {
    const query = state.query.trim().toLocaleLowerCase();
    return state.channels.filter((channel) => {
      if (channel.removed !== state.showRemoved) return false;
      if (state.group && channel.group !== state.group) return false;
      if (state.unmatchedOnly && hasGuideMatch(channel)) return false;
      if (!query) return true;
      const source = state.sources.get(channel.sourceId);
      const guideName = source && source.channels.get(channel.epgId);
      return `${channel.name} ${channel.epgId} ${channel.group} ${guideName || ""}`.toLocaleLowerCase().includes(query);
    }).sort((a, b) =>
      CHANNEL_COLLATOR.compare(a.group, b.group) ||
      CHANNEL_COLLATOR.compare(a.name, b.name)
    );
  }

  function createLogo(url, name, className) {
    const wrapper = document.createElement("span");
    wrapper.className = className || "channel-logo";
    if (url) {
      const image = document.createElement("img");
      image.src = url;
      image.alt = "";
      image.loading = "lazy";
      image.addEventListener("error", () => {
        wrapper.textContent = initials(name);
      }, { once: true });
      wrapper.append(image);
    } else {
      wrapper.textContent = initials(name);
    }
    return wrapper;
  }

  function initials(name) {
    return String(name || "?").trim().split(/\s+/).slice(0, 2).map((part) => part[0] || "").join("").toUpperCase();
  }

  function renderList() {
    const matches = filteredChannels();
    const maxPage = Math.max(0, Math.ceil(matches.length / PAGE_SIZE) - 1);
    state.page = Math.min(state.page, maxPage);
    const start = state.page * PAGE_SIZE;
    const pageChannels = matches.slice(start, start + PAGE_SIZE);
    ui.visibleCount.textContent = matches.length.toLocaleString();
    ui.pageLabel.textContent = matches.length
      ? `${(start + 1).toLocaleString()}–${Math.min(start + PAGE_SIZE, matches.length).toLocaleString()} of ${matches.length.toLocaleString()}`
      : "No channels";
    ui.previousPage.disabled = state.page === 0;
    ui.nextPage.disabled = state.page >= maxPage;
    ui.channelList.replaceChildren();

    if (!pageChannels.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = state.channels.length
        ? "No channels match these filters."
        : "This playlist has no channels.";
      ui.channelList.append(empty);
      return;
    }

    const fragment = document.createDocumentFragment();
    let lastGroup = null;
    pageChannels.forEach((channel) => {
      if (channel.group !== lastGroup) {
        const groupHeading = document.createElement("div");
        groupHeading.className = "channel-group-heading";
        groupHeading.textContent = channel.group;
        fragment.append(groupHeading);
        lastGroup = channel.group;
      }
      const button = document.createElement("button");
      button.type = "button";
      button.className = "channel-row";
      button.setAttribute("aria-current", channel.key === state.selectedKey ? "true" : "false");
      button.addEventListener("click", () => {
        state.selectedKey = channel.key;
        renderList();
        renderEditor();
      });
      button.append(createLogo(channel.logo, channel.name));

      const info = document.createElement("span");
      info.className = "channel-info";
      const name = document.createElement("span");
      name.className = "channel-name";
      name.textContent = channel.name;
      const subline = document.createElement("span");
      subline.className = "channel-subline";
      subline.textContent = channel.epgId ? `${channel.group} · ${channel.epgId}` : channel.group;
      info.append(name, subline);
      const matched = hasGuideMatch(channel);
      const indicator = document.createElement("span");
      indicator.className = `match-indicator${matched ? " is-matched" : ""}`;
      indicator.setAttribute("aria-label", matched ? "EPG matched" : "No matching EPG channel");
      button.append(info, indicator);
      fragment.append(button);
    });
    ui.channelList.append(fragment);
  }

  function makeInput(label, value, placeholder) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value;
    input.placeholder = placeholder || "";
    input.setAttribute("aria-label", label);
    return input;
  }

  function renderEditor() {
    const channel = getCurrentChannel();
    ui.channelEditor.replaceChildren();
    if (!channel) {
      const empty = document.createElement("div");
      empty.className = "empty-editor";
      const icon = document.createElement("span");
      icon.className = "empty-editor-icon";
      icon.textContent = "◉";
      const title = document.createElement("h2");
      title.textContent = "Select a channel";
      const description = document.createElement("p");
      description.textContent = "Choose a channel from your playlist to edit its name and EPG match.";
      empty.append(icon, title, description);
      ui.channelEditor.append(empty);
      renderNowPlaying();
      return;
    }

    if (channel.removed) {
      const removed = document.createElement("div");
      removed.className = "removed-editor";
      const title = document.createElement("h2");
      title.textContent = `${channel.name} is removed`;
      const description = document.createElement("p");
      description.textContent = "This channel will be omitted from the exported playlist and the next daily playlist build.";
      const restoreButton = document.createElement("button");
      restoreButton.type = "button";
      restoreButton.className = "button button-muted";
      restoreButton.textContent = "Restore channel";
      restoreButton.addEventListener("click", () => {
        channel.removed = false;
        state.showRemoved = false;
        ui.removedFilter.checked = false;
        persistEdits();
        renderList();
        renderEditor();
        updateStats();
      });
      removed.append(createLogo(channel.logo, channel.name, "channel-logo"), title, description, restoreButton);
      ui.channelEditor.append(removed);
      renderNowPlaying();
      return;
    }

    const header = document.createElement("div");
    header.className = "editor-header";
    header.append(createLogo(channel.logo, channel.name));
    const title = document.createElement("div");
    title.className = "editor-title";
    const heading = document.createElement("h2");
    heading.textContent = channel.name;
    const subtitle = document.createElement("p");
    subtitle.textContent = `${channel.group} · ${channel.epgId || "No EPG ID assigned"}`;
    title.append(heading, subtitle);
    header.append(title);

    const nameField = document.createElement("div");
    nameField.className = "form-field";
    const nameLabel = document.createElement("label");
    nameLabel.textContent = "Channel name";
    const nameInput = makeInput("Channel name", channel.name, "Enter a display name");
    nameInput.className = "field-control";
    nameInput.addEventListener("input", () => {
      channel.name = nameInput.value;
      heading.textContent = channel.name || "Untitled channel";
      renderList();
      updateStats();
    });
    nameInput.addEventListener("change", persistEdits);
    nameField.append(nameLabel, nameInput);

    const matchField = document.createElement("div");
    matchField.className = "form-field";
    const currentSource = state.sources.get(channel.sourceId);
    const currentGuideName = currentSource && currentSource.channels.get(channel.epgId);
    const matchLabel = document.createElement("label");
    matchLabel.textContent = "Match to EPG channel";
    const matchHint = document.createElement("span");
    matchHint.className = "field-hint";
    matchHint.textContent = currentGuideName && currentSource
      ? `Matched: ${currentGuideName} · ${currentSource.name}`
      : channel.epgId
        ? `No loaded match · ${channel.epgId} · ${currentSource ? currentSource.name : channel.sourceId}`
        : "Search all loaded EPG sources";
    matchLabel.append(matchHint);
    const matchRow = document.createElement("div");
    matchRow.className = "match-search-row";
    const matchInput = makeInput("Search EPG channel", currentGuideName || channel.epgId, "Search guide channels…");
    if (channel.epgId && currentGuideName) matchInput.classList.add("has-match");
    const clearButton = document.createElement("button");
    clearButton.type = "button";
    clearButton.className = "clear-match";
    clearButton.textContent = "Clear";
    clearButton.addEventListener("click", () => {
      channel.epgId = "";
      persistEdits();
      renderEditor();
      renderList();
      updateStats();
    });
    matchRow.append(matchInput, clearButton);
    const results = document.createElement("div");
    results.className = "match-results";
    const renderSuggestions = () => {
      results.replaceChildren();
      const query = matchInput.value.trim().toLocaleLowerCase();
      if (!query) return;
      const choices = [];
      sortedSources().forEach((source) => {
        source.channels.forEach((displayName, id) => {
          if (`${displayName} ${id} ${source.name}`.toLocaleLowerCase().includes(query)) {
            choices.push({ id, displayName, sourceId: source.id, sourceName: source.name });
          }
        });
      });
      choices.sort((a, b) => {
        const aExact = a.id.toLocaleLowerCase() === query ||
          a.displayName.toLocaleLowerCase() === query ||
          a.sourceName.toLocaleLowerCase() === query;
        const bExact = b.id.toLocaleLowerCase() === query ||
          b.displayName.toLocaleLowerCase() === query ||
          b.sourceName.toLocaleLowerCase() === query;
        return Number(bExact) - Number(aExact) ||
          CHANNEL_COLLATOR.compare(a.displayName, b.displayName) ||
          CHANNEL_COLLATOR.compare(a.sourceName, b.sourceName) ||
          CHANNEL_COLLATOR.compare(a.id, b.id);
      });
      choices.slice(0, 8).forEach(({ id, displayName, sourceId, sourceName }) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "match-option";
        const name = document.createElement("span");
        name.textContent = displayName;
        const details = document.createElement("span");
        details.textContent = `${sourceName} · ${id}`;
        option.addEventListener("click", () => {
          channel.epgId = id;
          channel.sourceId = sourceId;
          persistEdits();
          renderEditor();
          renderList();
          updateStats();
        });
        option.append(name, details);
        results.append(option);
      });
      if (!choices.length) {
        const noMatches = document.createElement("span");
        noMatches.className = "no-matches";
        noMatches.textContent = "No channel matches in the loaded guides. Enter an ID and press Enter to use it anyway.";
        results.append(noMatches);
      }
    };
    matchInput.addEventListener("input", renderSuggestions);
    matchInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && matchInput.value.trim()) {
        event.preventDefault();
        channel.epgId = matchInput.value.trim();
        persistEdits();
        renderEditor();
        renderList();
        updateStats();
      }
    });
    renderSuggestions();
    matchField.append(matchLabel, matchRow, results);

    const note = document.createElement("p");
    note.className = "editor-save-note";
    note.textContent = "Export the daily config to keep these edits across playlist rebuilds.";
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "button button-danger";
    removeButton.textContent = "Remove from playlist";
    removeButton.disabled = channel.endLineIndex === channel.lineIndex;
    if (removeButton.disabled) removeButton.title = "This channel has no stream URL to remove.";
    removeButton.addEventListener("click", () => {
      if (!window.confirm(`Remove "${channel.name}" from the playlist? Export the config to apply this to future builds.`)) return;
      channel.removed = true;
      state.showRemoved = false;
      ui.removedFilter.checked = false;
      persistEdits();
      renderList();
      renderEditor();
      updateStats();
    });
    const editorActions = document.createElement("div");
    editorActions.className = "editor-actions";
    editorActions.append(removeButton);
    ui.channelEditor.append(header, nameField, matchField, note, editorActions);
    renderNowPlaying();
  }

  function formatTime(timestamp) {
    return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(timestamp));
  }

  function renderNowPlaying() {
    const channel = getCurrentChannel();
    const now = Date.now();
    ui.guideClock.textContent = new Intl.DateTimeFormat(undefined, {
      hour: "numeric", minute: "2-digit", timeZoneName: "short"
    }).format(new Date(now));
    ui.nowPlaying.replaceChildren();

    if (!channel) {
      const placeholder = document.createElement("span");
      placeholder.className = "guide-placeholder";
      placeholder.textContent = "Select a channel to see its current programme.";
      ui.nowPlaying.append(placeholder);
      return;
    }
    if (channel.removed) {
      const placeholder = document.createElement("span");
      placeholder.className = "guide-placeholder";
      placeholder.textContent = "This channel is removed from the playlist.";
      ui.nowPlaying.append(placeholder);
      return;
    }

    const source = state.sources.get(channel.sourceId);
    const guideName = source && source.channels.get(channel.epgId);
    if (!channel.epgId || !source || !guideName) {
      const placeholder = document.createElement("span");
      placeholder.className = "guide-placeholder";
      placeholder.textContent = !channel.epgId
        ? "Assign an EPG channel to see what’s on now."
        : source ? "This EPG ID is not in the selected guide." : "Load this channel’s EPG source to see its guide.";
      ui.nowPlaying.append(placeholder);
      return;
    }

    const result = getProgram(channel, now);
    if (!result) {
      const placeholder = document.createElement("span");
      placeholder.className = "guide-placeholder";
      placeholder.textContent = `No programme data for ${guideName} at this time.`;
      ui.nowPlaying.append(placeholder);
      return;
    }
    const { item, isNext } = result;
    const label = document.createElement("span");
    label.className = "programme-label";
    label.textContent = isNext ? "Up next" : "On now";
    const title = document.createElement("h3");
    title.className = "programme-title";
    title.textContent = item.title;
    const meta = document.createElement("div");
    meta.className = "programme-meta";
    const time = document.createElement("span");
    time.textContent = `${formatTime(item.start)} – ${formatTime(item.stop)}`;
    const endTime = document.createElement("span");
    endTime.textContent = `${guideName} · ${source.name}`;
    meta.append(time, endTime);
    ui.nowPlaying.append(label, title, meta);

    if (!isNext) {
      const progress = document.createElement("div");
      progress.className = "progress-track";
      const fill = document.createElement("div");
      fill.className = "progress-fill";
      fill.style.width = `${Math.max(0, Math.min(100, ((now - item.start) / (item.stop - item.start)) * 100))}%`;
      progress.append(fill);
      ui.nowPlaying.append(progress);
    }
    if (item.description) {
      const description = document.createElement("p");
      description.className = "programme-description";
      description.textContent = item.description;
      ui.nowPlaying.append(description);
    }
  }

  function replaceAttribute(line, name, value) {
    const escaped = value.replace(/"/g, "&quot;");
    const pattern = new RegExp(`(^|\\s)${name}="[^"]*"`, "i");
    if (pattern.test(line)) {
      return line.replace(pattern, (_match, prefix) => `${prefix}${name}="${escaped}"`);
    }
    const parts = splitExtinf(line);
    return `${parts.metadata} ${name}="${escaped}",${parts.name}`;
  }

  function removeAttribute(line, name) {
    return line.replace(new RegExp(`\\s${name}="[^"]*"`, "i"), "");
  }

  function exportPlaylist() {
    if (!state.playlistLines.length) {
      setStatus("Load a playlist before exporting.", true);
      return;
    }
    if (state.channels.some((channel) =>
      channel.removed && channel.endLineIndex === channel.lineIndex
    )) {
      setStatus("Cannot export a removal for a channel with no stream URL.", true);
      return;
    }
    const lines = state.playlistLines.slice();
    const removals = [];
    state.channels.forEach((channel) => {
      if (channel.removed) {
        removals.push(channel);
        return;
      }
      let line = lines[channel.lineIndex];
      if (channel.name !== channel.originalName) {
        line = replaceAttribute(line, "tvg-name", channel.name);
        const parts = splitExtinf(line);
        line = `${parts.metadata},${channel.name}`;
      }
      if (channel.epgId !== channel.originalEpgId) {
        if (channel.epgId) line = replaceAttribute(line, "tvg-id", channel.epgId);
        else line = removeAttribute(line, "tvg-id");
      }
      if (channel.sourceId !== channel.originalSourceId) {
        if (channel.sourceId === "epgstrong") line = removeAttribute(line, "epg-source");
        else line = replaceAttribute(line, "epg-source", channel.sourceId);
      }
      lines[channel.lineIndex] = line;
    });
    removals.sort((a, b) => b.lineIndex - a.lineIndex).forEach((channel) => {
      lines.splice(channel.lineIndex, channel.endLineIndex - channel.lineIndex + 1);
    });
    downloadBlob(new Blob([`${lines.join("\r\n")}\r\n`], { type: "audio/x-mpegurl;charset=utf-8" }), "wes-edited.m3u");
    setStatus("Exported wes-edited.m3u", false);
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function exportConfiguration() {
    if (!state.playlistLines.length) {
      setStatus("Load a playlist before exporting its config.", true);
      return;
    }
    const records = new Map(
      state.configuration.channels.map((record) => [record.id, record])
    );
    state.channels.forEach((channel) => {
      const changes = {};
      if (channel.name !== channel.originalName) changes.name = channel.name;
      if (channel.epgId !== channel.originalEpgId) changes.tvg_id = channel.epgId;
      if (channel.sourceId !== channel.originalSourceId) changes.epg_source = channel.sourceId;
      const id = channel.studioId || identityId(channel.identity);
      if (Object.keys(changes).length || channel.removed) {
        const record = { id, match: channel.identity, set: changes };
        if (channel.removed) record.remove = true;
        records.set(id, record);
      } else {
        records.delete(id);
      }
    });
    const channels = Array.from(records.values());
    const config = { version: 1, playlist: "*", channels };
    downloadBlob(
      new Blob([`${JSON.stringify(config, null, 2)}\n`], { type: "application/json;charset=utf-8" }),
      "edits.json"
    );
    setStatus(`Exported edits.json · ${channels.length.toLocaleString()} channel config records`, false);
  }

  async function loadPlaylistFile(file) {
    if (!file) return;
    setStatus(`Reading ${file.name}…`, false);
    try {
      const parsed = parsePlaylist(await file.text());
      state.playlistLines = parsed.lines;
      state.channels = parsed.channels;
      state.playlistName = file.name;
      state.selectedKey = null;
      state.page = 0;
      if (state.configuration.playlist === "*" || state.configuration.playlist === state.playlistName) {
        applyExistingConfiguration(state.configuration);
      } else {
        state.configuration = { version: 1, playlist: state.playlistName, channels: [] };
      }
      restoreMarkerIdentities();
      restoreEdits();
      updateGroupFilter();
      updateStats();
      renderList();
      renderEditor();
      setStatus(`Loaded ${file.name} · ${state.channels.length.toLocaleString()} channels`, false);
    } catch (error) {
      setStatus(`Could not load playlist: ${error.message}`, true);
    }
  }

  async function loadGuideFile(file) {
    setStatus(`Reading guide ${file.name}…`, false);
    try {
      const text = await file.text();
      const id = `segment-${hashText(file.name.toLocaleLowerCase())}`;
      addGuide(text, id, `Segment · ${file.name}`);
      setStatus(`Loaded ${file.name} · ${state.sources.get(id).channels.size.toLocaleString()} EPG channels`, false);
    } catch (error) {
      setStatus(`Could not load ${file.name}: ${error.message}`, true);
    }
  }

  async function loadEpgstrongUrl() {
    const url = ui.epgstrongUrl.value.trim();
    if (!url) {
      setStatus("Enter the EPGStrong XML URL first.", true);
      ui.epgstrongUrl.focus();
      return;
    }
    let parsedUrl;
    try {
      parsedUrl = new URL(url, window.location.href);
    } catch (error) {
      setStatus(`Invalid EPGStrong URL: ${error.message}`, true);
      return;
    }
    if (!["http:", "https:"].includes(parsedUrl.protocol)) {
      setStatus("EPGStrong URL must use HTTP or HTTPS.", true);
      return;
    }
    setStatus("Loading EPGStrong guide…", false);
    try {
      const response = await fetch(parsedUrl.href, { cache: "no-cache" });
      if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
      const text = await response.text();
      addGuide(text, "epgstrong", `EPGStrong · ${parsedUrl.host}`);
      setStatus(`Loaded EPGStrong · ${state.sources.get("epgstrong").channels.size.toLocaleString()} EPG channels`, false);
    } catch (error) {
      setStatus(`Could not load EPGStrong: ${error.message}. Check the URL and host availability.`, true);
    }
  }

  async function loadRemoteSegment() {
    const filename = ui.remoteSegment.value;
    if (!REMOTE_SEGMENTS.includes(filename)) {
      setStatus("Choose a hosted EPG segment first.", true);
      return;
    }
    setStatus(`Loading ${filename}…`, false);
    try {
      const response = await fetch(`${TV_BASE_URL}/Temp/${filename}`, { cache: "no-cache" });
      if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
      const id = `segment-${hashText(filename.toLocaleLowerCase())}`;
      addGuide(await response.text(), id, `Segment · ${filename}`);
      setStatus(`Loaded ${filename} · ${state.sources.get(id).channels.size.toLocaleString()} EPG channels`, false);
    } catch (error) {
      setStatus(`Could not load ${filename}: ${error.message}`, true);
    }
  }

  async function loadAllRemoteSegments() {
    const queue = REMOTE_SEGMENTS.slice();
    const failures = [];
    let loaded = 0;
    setStatus(`Loading all ${REMOTE_SEGMENTS.length} hosted EPG segments…`, false);

    const worker = async () => {
      while (queue.length) {
        const filename = queue.shift();
        try {
          const response = await fetch(`${TV_BASE_URL}/Temp/${filename}`, { cache: "no-cache" });
          if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
          const id = `segment-${hashText(filename.toLocaleLowerCase())}`;
          addGuide(await response.text(), id, `Segment · ${filename}`, false);
          loaded += 1;
          if (loaded % 8 === 0) setStatus(`Loaded ${loaded}/${REMOTE_SEGMENTS.length} EPG segments…`, false);
        } catch (error) {
          failures.push(`${filename}: ${error.message}`);
        }
      }
    };

    await Promise.all(Array.from({ length: 6 }, worker));
    if (loaded) {
      updateSourceUi();
      renderList();
      renderEditor();
      updateStats();
    }
    return { loaded, failures };
  }

  async function tryLoadDefault(path, loader) {
    try {
      const response = await fetch(path, { cache: "no-cache" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await loader(await response.text());
      return { loaded: true, error: null };
    } catch (error) {
      return { loaded: false, error };
    }
  }

  async function initialize() {
    try {
      const playlistResult = await tryLoadDefault(PLAYLIST_URL, async (text) => {
        const parsed = parsePlaylist(text);
        state.playlistLines = parsed.lines;
        state.channels = parsed.channels;
      });
      let configError = null;
      if (playlistResult.loaded) {
        try {
          const response = await fetch(EDITS_URL, { cache: "no-cache" });
          if (response.ok) {
            applyExistingConfiguration(JSON.parse(await response.text()));
          } else if (response.status !== 404) {
            throw new Error(`HTTP ${response.status} ${response.statusText}`);
          }
        } catch (error) {
          configError = error;
          restoreMarkerIdentities();
        }
        if (!configError) restoreMarkerIdentities();
        restoreEdits();
        updateGroupFilter();
        updateStats();
        renderList();
        renderEditor();
      }
      const guideResult = await tryLoadDefault(EPGSTRONG_URL, async (text) => {
        addGuide(text, "epgstrong", "EPGStrong");
      });
      const segmentResult = playlistResult.loaded
        ? await loadAllRemoteSegments()
        : { loaded: 0, failures: [] };
      if (!playlistResult.loaded) {
        setStatus(`Could not load ${PLAYLIST_URL}: ${playlistResult.error.message}. Open a playlist file to get started.`, true);
      } else {
        const problems = [];
        if (!guideResult.loaded) {
          problems.push(`could not load EPGStrong.xml: ${guideResult.error.message}`);
        }
        if (configError) problems.push(`could not load edits.json: ${configError.message}`);
        if (segmentResult.failures.length) {
          problems.push(
            `loaded ${segmentResult.loaded}/${REMOTE_SEGMENTS.length} segments; failed: ${segmentResult.failures.join(", ")}`
          );
        }
        if (problems.length) {
          setStatus(`Playlist loaded; ${problems.join("; ")}.`, true);
        } else {
          setStatus(
            `Loaded strong.m3u · ${state.channels.length.toLocaleString()} channels · EPGStrong and all ${segmentResult.loaded} segments`,
            false
          );
        }
      }
    } catch (error) {
      setStatus(`Could not initialize the editor: ${error.message}`, true);
    }
  }

  ui.playlistFile.addEventListener("change", () => loadPlaylistFile(ui.playlistFile.files[0]));
  ui.guideFiles.addEventListener("change", async () => {
    const files = Array.from(ui.guideFiles.files || []);
    for (const file of files) await loadGuideFile(file);
    ui.guideFiles.value = "";
  });
  ui.exportPlaylist.addEventListener("click", exportPlaylist);
  ui.exportConfig.addEventListener("click", exportConfiguration);
  ui.loadEpgstrong.addEventListener("click", loadEpgstrongUrl);
  REMOTE_SEGMENTS.forEach((filename) => ui.remoteSegment.add(new Option(filename, filename)));
  ui.loadRemoteSegment.addEventListener("click", loadRemoteSegment);
  ui.epgstrongUrl.addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadEpgstrongUrl();
  });
  ui.channelSearch.addEventListener("input", () => {
    state.query = ui.channelSearch.value;
    state.page = 0;
    renderList();
  });
  ui.groupFilter.addEventListener("change", () => {
    state.group = ui.groupFilter.value;
    state.page = 0;
    renderList();
  });
  ui.unmatchedFilter.addEventListener("change", () => {
    state.unmatchedOnly = ui.unmatchedFilter.checked;
    state.page = 0;
    renderList();
  });
  ui.removedFilter.addEventListener("change", () => {
    state.showRemoved = ui.removedFilter.checked;
    state.selectedKey = null;
    state.page = 0;
    renderList();
    renderEditor();
  });
  ui.previousPage.addEventListener("click", () => {
    state.page = Math.max(0, state.page - 1);
    renderList();
  });
  ui.nextPage.addEventListener("click", () => {
    state.page += 1;
    renderList();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      ui.channelSearch.focus();
    }
  });
  window.setInterval(renderNowPlaying, 60_000);
  initialize();
})();
