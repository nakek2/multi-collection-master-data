#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_master_sources.py
===========================
各収集アイテムの情報源サイトを定期的にチェックし、更新(新規追加等)を
検知したらGitHub Issueを自動作成する。GitHub Actionsから定期実行される
ことを想定している(.github/workflows/monitor_sources.yml)。

【対象外にしたサイト】
・御刻印(roadmania-japan.com)、マンホールカードのまとめサイト(anythingsearch.info)は
  robots.txtで自動アクセスを明示的に禁止しているため、このスクリプトの対象外とした。
  御刻印はX/Instagram公式アカウントの通知機能で代替する運用とする。

【各サイトの監視方式】
・峠ステッカー(tohge-project.jp)    : ページ内の峠id一覧を抽出し、新規idを検知
・国道ステッカー(vcountry.jp)       : ページ内の店舗id一覧を抽出し、新規idを検知
・御船印(gosen-in.jp)               : ページ内に明記されている「最終更新日」を監視
・御翔印(ec.jal.co.jp)              : 店舗一覧PDFへのリンクURLの変化を監視
・マンホールカード(gk-p.jp)         : 「第◯弾」表示、および全種一覧PDFリンクの変化を監視

差分を検知した場合のみGitHub Issueを作成する(変化が無ければ何もしない)。
状態(前回チェック時の値)は monitor_state.json に保存し、Actions側で
リポジトリへコミットして次回実行時に引き継ぐ。
"""
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests

STATE_FILE = Path(__file__).parent / "monitor_state.json"
USER_AGENT = "Mozilla/5.0 (compatible; MultiCollectionAppMonitor/1.0)"
TIMEOUT = 15


@dataclass
class CheckResult:
    source_name: str
    signal: dict  # 保存・比較する値(JSON化可能な辞書)
    summary: str  # 変化検知時にIssueへ書く要約(人間向け)
    ok: bool = True
    error: str | None = None


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def _diagnose(html: str, marker: str) -> str:
    """抽出に失敗した際、原因調査用の情報をエラーメッセージに含める。
    ・取得できたページの文字数
    ・目印となる文字列(marker)がページ内に何回出現するか
    ・出現していれば、その前後の生の中身(タグ構造の確認用)
    """
    count = html.count(marker)
    lines = [
        "ページ構造が変わった可能性があります(抽出できませんでした)。",
        f"取得ページの文字数: {len(html)}",
        f"目印文字列 '{marker}' の出現回数: {count}",
    ]
    if count > 0:
        idx = html.find(marker)
        snippet = html[max(0, idx - 100): idx + 300]
        lines.append(f"出現箇所の前後200文字:\n{snippet}")
    else:
        lines.append(
            "目印文字列が1回も見つかりませんでした。"
            "JavaScriptで動的に描画されるページの可能性があります"
            "(単純なHTTP取得では中身が空のことがあります)。"
        )
        lines.append(f"取得できたページの先頭500文字:\n{html[:500]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 峠ステッカー: tohge-project.jp
# ---------------------------------------------------------------------------
def check_touge() -> CheckResult:
    url = "https://www.tohge-project.jp/"
    try:
        html = fetch_html(url)
    except Exception as e:
        return CheckResult("峠ステッカー", {}, "", ok=False, error=str(e))

    # <a href="https://www.tohge-project.jp/tohge/?id=162">...alt="迷ヶ平"...</a>
    # のようなパターンから id と名前(alt)を抽出する。
    pattern = re.compile(
        r'tohge/\?id=(\d+)"[^>]*>\s*<img[^>]*alt="([^"]*)"',
        re.IGNORECASE,
    )
    matches = pattern.findall(html)
    if not matches:
        # 代替パターン(alt側が先に来る/属性順が違う場合)にも対応する。
        pattern2 = re.compile(
            r'<img[^>]*alt="([^"]*)"[^>]*>\s*(?:</a>)?\s*<a[^>]*tohge/\?id=(\d+)',
            re.IGNORECASE,
        )
        matches = [(m[1], m[0]) for m in pattern2.findall(html)]

    id_to_name = {m[0]: m[1] for m in matches}
    if not id_to_name:
        return CheckResult(
            "峠ステッカー", {}, "",
            ok=False,
            error="ページ構造が変わった可能性があります(id一覧を抽出できませんでした)",
        )

    signal = {"ids": id_to_name}
    return CheckResult("峠ステッカー", signal, "")


def diff_touge(old_signal: dict, new_signal: dict) -> str | None:
    old_ids = set((old_signal or {}).get("ids", {}).keys())
    new_ids_map = new_signal.get("ids", {})
    new_ids = set(new_ids_map.keys())
    added = new_ids - old_ids
    if not added:
        return None
    lines = [f"- id={i}: {new_ids_map[i]}" for i in sorted(added, key=int)]
    return "新しい峠が追加された可能性があります:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# 国道ステッカー: vcountry.jp
# ---------------------------------------------------------------------------
def check_kokudo() -> CheckResult:
    url = "https://vcountry.jp/kokudou/map/list.aspx"
    try:
        html = fetch_html(url)
    except Exception as e:
        return CheckResult("国道ステッカー", {}, "", ok=False, error=str(e))

    # 実際のHTML構造(2026-08-27、実機ログで確認済み):
    # <a href='./default.aspx?id=50' class='a-blue'><img src='...' alt='1号' />
    #   <dl><dt>神奈川県</dt><dd><h2>道の駅 箱根峠</h2></dd></dl></a>
    # シングルクォート、店名はh2タグの中、路線番号はimgのalt属性という
    # 想定と異なる構造だったため、これに合わせて書き直した。
    pattern = re.compile(
        r"<a href='\./default\.aspx\?id=(\d+)'[^>]*>.*?alt='([^']*)'.*?<dt>([^<]*)</dt><dd><h2>([^<]*)</h2>",
        re.DOTALL,
    )
    matches = pattern.findall(html)

    id_to_label = {}
    for spot_id, route_no, prefecture, store_name in matches:
        id_to_label[spot_id] = f"{route_no} {prefecture}{store_name}"

    if not id_to_label:
        return CheckResult(
            "国道ステッカー", {}, "",
            ok=False,
            error=_diagnose(html, "default.aspx?id="),
        )

    signal = {"ids": id_to_label}
    return CheckResult("国道ステッカー", signal, "")


def diff_kokudo(old_signal: dict, new_signal: dict) -> str | None:
    old_ids = set((old_signal or {}).get("ids", {}).keys())
    new_ids_map = new_signal.get("ids", {})
    new_ids = set(new_ids_map.keys())
    added = new_ids - old_ids
    if not added:
        return None
    lines = [f"- id={i}: {new_ids_map[i]}" for i in sorted(added, key=int)]
    return "新しい店舗/番号が追加された可能性があります:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# 御船印: gosen-in.jp
# ---------------------------------------------------------------------------
def check_gosenin() -> CheckResult:
    url = "https://gosen-in.jp/member_list.php"
    try:
        html = fetch_html(url)
    except Exception as e:
        return CheckResult("御船印", {}, "", ok=False, error=str(e))

    # ページ上部に明記されている「最終更新日：08月20日」を抽出する。
    # 「最終更新日」の出現位置を先に見つけ、その直後の一定範囲内から
    # 日付パターンだけを探す2段階方式にする(タグ等が間に挟まっても頑健)。
    idx = html.find("最終更新日")
    m = None
    if idx != -1:
        window = html[idx: idx + 200]
        m = re.search(r"(\d{1,2}月\d{1,2}日)", window)
    if not m:
        return CheckResult(
            "御船印", {}, "",
            ok=False,
            error=_diagnose(html, "最終更新日"),
        )

    signal = {"last_updated": m.group(1)}
    return CheckResult("御船印", signal, "")


def diff_gosenin(old_signal: dict, new_signal: dict) -> str | None:
    old_date = (old_signal or {}).get("last_updated")
    new_date = new_signal.get("last_updated")
    if old_date is None or old_date == new_date:
        return None
    return f"ページ内の「最終更新日」が変わりました: {old_date} → {new_date}\n参加社リストをご確認ください。"


# ---------------------------------------------------------------------------
# 御翔印: ec.jal.co.jp (店舗一覧PDFへのリンクを監視)
# ---------------------------------------------------------------------------
def check_goshouin() -> CheckResult:
    url = "https://ec.jal.co.jp/shop/pages/0002soranogoshoin.aspx"
    try:
        html = fetch_html(url)
    except Exception as e:
        return CheckResult("御翔印", {}, "", ok=False, error=str(e))

    # 「お問い合わせ先一覧」PDFへのリンクを抽出する。
    m = re.search(r'href="([^"]+soranogoshoin[^"]*\.pdf[^"]*)"', html, re.IGNORECASE)
    if not m:
        return CheckResult(
            "御翔印", {}, "",
            ok=False,
            error="ページ構造が変わった可能性があります(PDFリンクを抽出できませんでした)",
        )

    signal = {"pdf_url": m.group(1)}
    return CheckResult("御翔印", signal, "")


def diff_goshouin(old_signal: dict, new_signal: dict) -> str | None:
    old_url = (old_signal or {}).get("pdf_url")
    new_url = new_signal.get("pdf_url")
    if old_url is None or old_url == new_url:
        return None
    return f"店舗一覧PDFのリンクが変わりました:\n旧: {old_url}\n新: {new_url}\nPDFの中身をご確認ください。"


# ---------------------------------------------------------------------------
# マンホールカード: gk-p.jp (公式サイト、robots.txt制限なし)
# ---------------------------------------------------------------------------
def check_manhole() -> CheckResult:
    url = "https://www.gk-p.jp/mhcard/"
    try:
        html = fetch_html(url)
    except Exception as e:
        return CheckResult("マンホールカード", {}, "", ok=False, error=str(e))

    # 「第29弾内容紹介」のような表記から弾数を抽出する。
    m = re.search(r"第(\d+)弾内容紹介", html)
    batch = m.group(1) if m else None

    # 「全種一覧」PDFへのリンクも合わせて監視する(ファイル名に弾数が入っている想定)。
    m2 = re.search(r'href="([^"]*全種一覧[^"]*|[^"]*tool3\.pdf[^"]*)"', html)
    all_list_pdf = m2.group(1) if m2 else None

    if batch is None and all_list_pdf is None:
        return CheckResult(
            "マンホールカード", {}, "",
            ok=False,
            error="ページ構造が変わった可能性があります(弾数/PDFリンクを抽出できませんでした)",
        )

    signal = {"batch": batch, "all_list_pdf": all_list_pdf}
    return CheckResult("マンホールカード", signal, "")


def diff_manhole(old_signal: dict, new_signal: dict) -> str | None:
    old_signal = old_signal or {}
    messages = []
    if old_signal.get("batch") and new_signal.get("batch") and old_signal["batch"] != new_signal["batch"]:
        messages.append(f"弾数が変わりました: 第{old_signal['batch']}弾 → 第{new_signal['batch']}弾")
    if (
        old_signal.get("all_list_pdf")
        and new_signal.get("all_list_pdf")
        and old_signal["all_list_pdf"] != new_signal["all_list_pdf"]
    ):
        messages.append(
            f"全種一覧PDFのリンクが変わりました:\n旧: {old_signal['all_list_pdf']}\n新: {new_signal['all_list_pdf']}"
        )
    if not messages:
        return None
    return "\n".join(messages) + "\nマンホールカード公式サイトをご確認ください。"


# ---------------------------------------------------------------------------
# 共通処理
# ---------------------------------------------------------------------------
CHECKERS = [
    (check_touge, diff_touge),
    (check_kokudo, diff_kokudo),
    (check_gosenin, diff_gosenin),
    (check_goshouin, diff_goshouin),
    (check_manhole, diff_manhole),
]


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def create_github_issue(title: str, body: str) -> None:
    """GitHub Actions実行時のみ、GITHUB_TOKEN/GITHUB_REPOSITORYを使ってIssueを作成する。
    ローカル実行時(環境変数が無い場合)はコンソール出力のみで済ませる。"""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")  # 例: "nakek2/multi-collection-master-data"
    if not token or not repo:
        print(f"[通知(ローカル実行のためIssue作成はスキップ)] {title}\n{body}\n")
        return

    url = f"https://api.github.com/repos/{repo}/issues"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": title, "body": body, "labels": ["master-update-check"]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    print(f"Issueを作成しました: {resp.json().get('html_url')}")


def main() -> int:
    state = load_state()
    new_state = dict(state)
    any_error = False

    for checker, differ in CHECKERS:
        result = checker()
        if not result.ok:
            print(f"[警告] {result.source_name}: {result.error}", file=sys.stderr)
            any_error = True
            continue

        old_signal = state.get(result.source_name)
        diff_message = differ(old_signal, result.signal)

        if diff_message:
            create_github_issue(
                title=f"[マスタ更新検知] {result.source_name}",
                body=diff_message,
            )

        new_state[result.source_name] = result.signal

    save_state(new_state)

    # 個別サイトの取得失敗はワークフロー全体の失敗にはしない
    # (一時的なサイト不調で毎回赤くなるのを避けるため)。ログにのみ残す。
    return 0


if __name__ == "__main__":
    sys.exit(main())
