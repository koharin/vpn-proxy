// vpn-proxy 화면. 서버가 준 글자는 모두 textContent 로만 넣는다(HTML 로 해석하지 않는다).
"use strict";

const STATE_TEXT = { up: "연결됨", connecting: "연결 중", failed: "실패", off: "꺼짐", unknown: "알 수 없음" };
const list = document.getElementById("list");
const banner = document.getElementById("banner");
const notice = document.getElementById("notice");
const empty = document.getElementById("empty");
const updated = document.getElementById("updated");
const openLogs = new Set(); // 로그를 펼쳐 둔 VPN
const busy = new Set(); // 켜기·끄기 요청이 진행 중인 VPN
let timer = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function show(node, text) {
  node.textContent = text || "";
  node.hidden = !text;
}

function card(p, dockerOk) {
  const state = STATE_TEXT[p.state] ? p.state : "unknown";
  const box = el("article", "card");

  const head = el("div", "row");
  head.append(el("h2", "name", p.name));
  head.append(el("span", "badge s-" + state, STATE_TEXT[state]));
  head.append(el("span", "grow"));
  const running = state === "up" || state === "connecting" || state === "failed";
  const btn = el("button", running ? "off" : "", running ? "끄기" : "켜기");
  btn.type = "button";
  btn.disabled = !dockerOk || busy.has(p.id);
  btn.setAttribute("aria-label", p.name + (running ? " 끄기" : " 켜기"));
  btn.addEventListener("click", () => toggle(p, !running));
  head.append(btn);
  box.append(head);

  const meta = el("dl", "meta");
  const add = (k, v) => { meta.append(el("dt", "", k), el("dd", "", v)); };
  add("SOCKS5", p.socks_port ? (p.socks_host || "127.0.0.1") + ":" + p.socks_port : "포트 없음");
  let files = "폴더 없음";
  if (p.ovpn_files === 0) files = "ovpn 파일 없음";
  else if (p.ovpn_files === 1) files = ".ovpn 1개";
  else if (p.ovpn_files > 1) files = ".ovpn " + p.ovpn_files + "개(1개만 두어야 함)";
  add("ovpn 폴더", (p.folder || "-") + " — " + files);
  if (p.since && state !== "off") add("상태 시각", p.since);
  if (p.dns && p.dns.length) add("DNS", p.dns.join(", "));
  box.append(meta);

  if (p.message && state !== "off") box.append(el("p", "msg s-" + state, (state === "failed" ? "사유: " : "") + p.message));

  const det = el("details");
  det.open = openLogs.has(p.id);
  det.addEventListener("toggle", () => { if (det.open) openLogs.add(p.id); else openLogs.delete(p.id); });
  det.append(el("summary", "", "최근 로그 " + (p.log ? p.log.length : 0) + "줄"));
  det.append(el("pre", "", p.log && p.log.length ? p.log.join("\n") : "로그 없음"));
  box.append(det);
  return box;
}

async function refresh() {
  try {
    const res = await fetch("/api/profiles", { headers: { Accept: "application/json" } });
    const data = await res.json();
    show(banner, data.error || "");
    const profiles = Array.isArray(data.profiles) ? data.profiles : [];
    list.replaceChildren(...profiles.map((p) => card(p, data.docker === true)));
    empty.hidden = profiles.length > 0 || Boolean(data.error);
    updated.textContent = " · 갱신 " + new Date().toLocaleTimeString();
  } catch (e) {
    show(banner, "화면 서버에 연결하지 못했습니다. gui/server.py 가 떠 있는지 확인하세요.");
  }
}

async function toggle(p, on) {
  if (busy.has(p.id)) return;
  busy.add(p.id);
  show(notice, p.name + (on ? " 켜는 중…" : " 끄는 중…"));
  await refresh();
  try {
    const res = await fetch("/api/profiles/" + encodeURIComponent(p.id) + (on ? "/on" : "/off"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await res.json();
    show(notice, p.name + ": " + (data.message || (data.ok ? "완료" : "실패")));
  } catch (e) {
    show(notice, p.name + ": 요청을 보내지 못했습니다.");
  } finally {
    busy.delete(p.id);
    await refresh();
  }
}

function schedule() {
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) refresh(); }, 3000);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
refresh();
schedule();
