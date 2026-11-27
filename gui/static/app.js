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
const asking = new Set(); // 삭제 확인을 묻고 있는 VPN
const OVPN_MAX = 256 * 1024;
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

  // 제목 = .ovpn 파일 이름, 부제 = 접속하는 VPN 서버. 폴더 이름과 로컬 포트는 작게 덧붙인다.
  const title = p.title || p.name;
  const head = el("div", "row");
  const names = el("div", "names");
  const top = el("div", "row");
  top.append(el("h2", "name", title));
  top.append(el("span", "badge s-" + state, STATE_TEXT[state]));
  names.append(top);
  let server = p.server || "서버 주소를 읽지 못함";
  if (p.server && p.server_more > 0) server += " 외 " + p.server_more + "곳";
  names.append(el("p", "server", server));
  names.append(el("p", "aux", p.name + " · 로컬 포트 " + (p.socks_port || "없음")));
  head.append(names);
  const running = state === "up" || state === "connecting" || state === "failed";
  const btn = el("button", running ? "off" : "", running ? "끄기" : "켜기");
  btn.type = "button";
  btn.disabled = !dockerOk || busy.has(p.id);
  btn.setAttribute("aria-label", title + (running ? " 끄기" : " 켜기"));
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

  // 웹에서 올린 VPN 만 지울 수 있다. 한 번 더 묻고 지운다.
  if (p.managed) {
    const row = el("p", "row danger");
    if (asking.has(p.id)) {
      row.append(el("span", "", p.imported
        ? "컨테이너를 멈추고 등록만 지웁니다. 직접 넣은 폴더(" + (p.folder || p.id) + ")와 그 안의 .ovpn 원본은 지우지 않습니다."
        : "컨테이너를 멈추고 올린 .ovpn 과 아이디·비밀번호 파일(" + (p.folder || p.id) + ")을 지웁니다. 되돌릴 수 없습니다."));
      const yes = el("button", "del", "삭제 확인");
      yes.type = "button";
      yes.disabled = !dockerOk || busy.has(p.id);
      yes.addEventListener("click", () => remove(p));
      const no = el("button", "off", "취소");
      no.type = "button";
      no.addEventListener("click", () => { asking.delete(p.id); refresh(); });
      row.append(yes, no);
    } else {
      const del = el("button", "off", "삭제");
      del.type = "button";
      del.disabled = !dockerOk || busy.has(p.id);
      del.setAttribute("aria-label", title + " 삭제");
      del.addEventListener("click", () => { asking.add(p.id); refresh(); });
      row.append(del);
    }
    box.append(row);
  }
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
  show(notice, (p.title || p.name) + (on ? " 켜는 중…" : " 끄는 중…"));
  await refresh();
  try {
    const res = await fetch("/api/profiles/" + encodeURIComponent(p.id) + (on ? "/on" : "/off"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await res.json();
    show(notice, (p.title || p.name) + ": " + (data.message || (data.ok ? "완료" : "실패")));
  } catch (e) {
    show(notice, (p.title || p.name) + ": 요청을 보내지 못했습니다.");
  } finally {
    busy.delete(p.id);
    await refresh();
  }
}

async function remove(p) {
  if (busy.has(p.id)) return;
  busy.add(p.id);
  show(notice, (p.title || p.name) + " 지우는 중…");
  try {
    const res = await fetch("/api/vpns/" + encodeURIComponent(p.id) + "/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: p.id }),
    });
    const data = await res.json();
    show(notice, (p.title || p.name) + ": " + (data.message || (data.ok ? "지웠습니다." : "실패")));
  } catch (e) {
    show(notice, (p.title || p.name) + ": 요청을 보내지 못했습니다.");
  } finally {
    busy.delete(p.id);
    asking.delete(p.id);
    await refresh();
  }
}

// ── VPN 추가 ────────────────────────────────────────────────────────────
const addFile = document.getElementById("add-file");
const addName = document.getElementById("add-name");
const addUser = document.getElementById("add-user");
const addPass = document.getElementById("add-pass");
const addReplace = document.getElementById("add-replace");
const addGo = document.getElementById("add-go");
const addMsg = document.getElementById("add-msg");
const addReasons = document.getElementById("add-reasons");

function addResult(text, reasons) {
  show(addMsg, text);
  const items = Array.isArray(reasons) ? reasons : [];
  addReasons.replaceChildren(...items.map((r) => el("li", "", String(r))));
  addReasons.hidden = items.length === 0;
}

// 파일 이름에서 이름 칸을 채운다(소문자·숫자·- 만 남긴다).
addFile.addEventListener("change", () => {
  const f = addFile.files && addFile.files[0];
  if (!f || addName.value) return;
  addName.value = f.name.replace(/\.ovpn$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24).replace(/-+$/, "");
});

async function upload() {
  const f = addFile.files && addFile.files[0];
  if (!f) return addResult("ovpn 파일을 고르세요.");
  if (f.size > OVPN_MAX) return addResult("ovpn 파일이 너무 큽니다(256KB 까지).");
  if (!/^[a-z0-9]([a-z0-9-]{0,22}[a-z0-9])?$/.test(addName.value)) return addResult("이름은 소문자·숫자·'-' 로 1~24자, 처음과 끝은 소문자나 숫자여야 합니다.");
  addGo.disabled = true;
  addResult("올리는 중…");
  try {
    const text = await f.text();
    const res = await fetch("/api/vpns", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: addName.value, ovpn: text, username: addUser.value, password: addPass.value, replace: addReplace.checked }),
    });
    const data = await res.json();
    addResult(data.message || (data.ok ? "등록했습니다." : "실패"), data.reasons);
    if (data.ok) { addFile.value = ""; addName.value = ""; addUser.value = ""; addReplace.checked = false; }
  } catch (e) {
    addResult("요청을 보내지 못했습니다.");
  } finally {
    addPass.value = ""; // 비밀번호는 화면에 남기지 않는다
    addGo.disabled = false;
    await refresh();
  }
}
addGo.addEventListener("click", upload);

function schedule() {
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) refresh(); }, 3000);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
refresh();
schedule();
