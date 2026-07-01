import streamlit as st
import anthropic
import base64
import json
import re
import os
from collections import Counter
from PIL import Image
import io
from datetime import datetime
import pandas as pd

st.set_page_config(
    page_title="KADO-LOGIC",
    page_icon="🌸",
    layout="centered",
)

# パスワード認証
def check_password():
    if st.session_state.get("authenticated"):
        return True
    st.markdown("## 🌸 KADO-LOGIC")
    pwd = st.text_input("パスワードを入力してください", type="password")
    if st.button("ログイン", type="primary"):
        correct = st.secrets.get("APP_PASSWORD", "kadologic2024")
        if pwd == correct:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    return False

if not check_password():
    st.stop()

st.markdown("""
<style>
    .stApp { background-color: #F5F0E8; }
    .issue-card {
        background: white;
        border-radius: 10px;
        padding: 14px 16px;
        margin: 8px 0;
        border-left: 4px solid #C75B4E;
    }
    .strength-card {
        background: white;
        border-radius: 10px;
        padding: 14px 16px;
        margin: 8px 0;
        border-left: 4px solid #4A7C59;
    }
    .state-label {
        font-size: 11px;
        color: #888;
        margin-bottom: 2px;
    }
    .state-value {
        font-size: 14px;
        font-weight: 500;
        color: #333;
    }
    .fix-text {
        background: #FFF8E1;
        border-radius: 6px;
        padding: 8px 10px;
        margin-top: 8px;
        font-size: 14px;
    }
</style>
""", unsafe_allow_html=True)

# データ保存先（ローカル fallback 用）
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
os.makedirs(IMAGES_DIR, exist_ok=True)


@st.cache_resource
def get_supabase():
    """Supabaseクライアントを返す。Secrets未設定の場合はNone（ローカル動作）。"""
    url = st.secrets.get("SUPABASE_URL", "")
    key = st.secrets.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception:
        return None


def load_sessions() -> list:
    sb = get_supabase()
    if sb:
        res = sb.table("sessions").select("*").order("session_number", desc=False).execute()
        return res.data
    if not os.path.exists(SESSIONS_FILE):
        return []
    with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_session(session: dict):
    sb = get_supabase()
    if sb:
        sb.table("sessions").insert(session).execute()
        return
    sessions = load_sessions()
    sessions.append(session)
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)


def save_image(image_bytes: bytes, session_id: str, ext: str = "jpg") -> str:
    filename = f"{session_id}.{ext}"
    sb = get_supabase()
    if sb:
        mime = "image/png" if ext == "png" else "image/jpeg"
        sb.storage.from_("kado-images").upload(filename, image_bytes, {"content-type": mime})
        return filename
    with open(os.path.join(IMAGES_DIR, filename), "wb") as f:
        f.write(image_bytes)
    return filename


def get_image_src(s: dict):
    """セッションから表示用の画像ソース（URLまたはローカルパス）を返す。"""
    filename = s.get("image_filename", "")
    if not filename:
        return None
    sb = get_supabase()
    if sb:
        return sb.storage.from_("kado-images").get_public_url(filename)
    path = os.path.join(IMAGES_DIR, filename)
    return path if os.path.exists(path) else None


def build_past_context(sessions: list) -> str:
    """直近5回のレッスン記録をテキスト化してプロンプトに渡す"""
    if not sessions:
        return ""
    lines = ["\n【過去のレッスン記録】"]
    for s in sessions[-5:]:
        date_str = datetime.fromisoformat(s["date"]).strftime("%Y/%m/%d")
        issues = [i["title"] for i in s.get("issues", [])]
        lines.append(
            f"レッスン{s['session_number']} ({date_str}): 総合{s.get('overall_score','?')}点\n"
            f"  花材: {', '.join(s.get('flower_materials', ['不明']))}\n"
            f"  課題: {', '.join(issues) if issues else 'なし'}"
        )
    return "\n".join(lines)


def analyze_image(image_bytes: bytes, api_key: str, past_sessions: list, mime_type: str) -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    session_num = len(past_sessions) + 1
    past_context = build_past_context(past_sessions)
    is_first = len(past_sessions) == 0

    progress_rule = (
        "これは初回レッスンです。progress_commentは「初回レッスンです。これがあなたの出発点になります。」"
        "recurring_issuesは空配列[]にしてください。"
        if is_first else
        "過去のレッスン記録と比較して改善点・継続課題を指摘してください。"
        "recurring_issuesには過去にも繰り返し出た課題のみ記載してください。"
    )

    prompt = f"""あなたは華道（いけばな）の専門家審査員です。
これはレッスン{session_num}回目の分析です。{past_context}

今回の生け花の画像を分析し、以下のJSON形式のみで回答してください。
他のテキストは一切含めず、JSONのみを返してください。

{{
  "flower_materials": ["画像から検出した花材1", "花材2"],
  "style": "盛り花",
  "overall_score": 75,
  "form_score": 72,
  "color_score": 80,
  "space_score": 70,
  "strengths": [
    {{
      "point": "しんの高さが適切",
      "detail": "花器の1.5倍程度で安定感がある"
    }}
  ],
  "issues": [
    {{
      "title": "そえの傾き不足",
      "current_state": "しんに対してほぼ垂直（約80度）",
      "ideal_state": "しんから45〜50度傾いた状態",
      "fix": "根元を左に5〜10度倒す",
      "priority": 1
    }}
  ],
  "progress_comment": "前回比+3点。足元の密度は改善。そえの傾きが継続課題。",
  "recurring_issues": ["そえの傾きが毎回浅い傾向"],
  "next_lesson_focus": "次回は最初にそえの傾きを決めてから他の花材を配置してみる"
}}

フィードバックのルール：
1. flower_materialsは画像に写っている花材を日本語で列挙する
2. issuesは「今の状態 → 理想の状態 → 具体的な直し方」の形式で必ず記述
3. 直し方は数値（cm・度・本数）を入れて、次のレッスンで再現できる表現にする
4. issuesは優先度順（priority 1が最重要）で最大3つ
5. strengthsは最大2つ、理由を具体的に添える
6. {progress_rule}"""

    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": image_data},
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )

    content = message.content[0].text
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        raise ValueError("AIのレスポンスを解析できませんでした")
    return json.loads(match.group(0))


def show_result(result: dict, image_bytes: bytes, session_num: int):
    """分析結果を左脳派向けの構造で表示"""

    # 花材（AI認識）
    materials = result.get("flower_materials", [])
    if materials:
        st.caption(f"🌿 検出花材: {'　・　'.join(materials)}")

    st.image(image_bytes, use_container_width=True)
    st.markdown("")

    # スコア
    overall = result.get("overall_score", 0)
    col_o, col_f, col_c, col_s = st.columns([2, 1, 1, 1])
    with col_o:
        st.metric("⭐ 総合評価", f"{overall} 点")
    with col_f:
        st.metric("形態", f"{result.get('form_score', 0)}点")
    with col_c:
        st.metric("色彩", f"{result.get('color_score', 0)}点")
    with col_s:
        st.metric("空間", f"{result.get('space_score', 0)}点")

    # 上達コメント
    progress = result.get("progress_comment", "")
    if progress:
        if session_num == 1:
            st.info(f"📌 {progress}")
        else:
            st.success(f"📈 {progress}")

    st.divider()

    # 改善ポイント（メインコンテンツ）
    issues = result.get("issues", [])
    if issues:
        st.markdown("#### 🔧 改善ポイント")
        for issue in sorted(issues, key=lambda x: x.get("priority", 99)):
            st.markdown(f"""
<div class="issue-card">
  <strong>{'🔴' if issue.get('priority')==1 else '🟡'} {issue['title']}</strong><br><br>
  <div style="display:flex; gap:16px;">
    <div style="flex:1;">
      <div class="state-label">今の状態</div>
      <div class="state-value">{issue.get('current_state','')}</div>
    </div>
    <div style="font-size:20px; align-self:center;">→</div>
    <div style="flex:1;">
      <div class="state-label">理想の状態</div>
      <div class="state-value">{issue.get('ideal_state','')}</div>
    </div>
  </div>
  <div class="fix-text">✏️ 直し方: {issue.get('fix','')}</div>
</div>
""", unsafe_allow_html=True)

    # 強み
    strengths = result.get("strengths", [])
    if strengths:
        st.markdown("#### ✅ できていること")
        for s in strengths:
            st.markdown(f"""
<div class="strength-card">
  <strong>{s['point']}</strong><br>
  <span style="color:#555; font-size:14px;">{s.get('detail','')}</span>
</div>
""", unsafe_allow_html=True)

    # 繰り返し課題
    recurring = result.get("recurring_issues", [])
    if recurring:
        st.divider()
        st.markdown("#### ⚠️ 繰り返し出ている癖")
        for r in recurring:
            st.warning(r)

    # 次回フォーカス
    focus = result.get("next_lesson_focus", "")
    if focus:
        st.divider()
        st.markdown("#### 🎯 次回レッスンで意識すること")
        st.success(focus)


# ============================================================
# レイアウト
# ============================================================

st.title("🌸 KADO-LOGIC")
st.caption("華道フィードバック＆上達記録アプリ")

# サイドバー：APIキー（secretsから自動取得、なければ手入力）
with st.sidebar:
    st.header("⚙️ 設定")
    api_key_from_secrets = st.secrets.get("ANTHROPIC_API_KEY", "")
    if api_key_from_secrets:
        api_key = api_key_from_secrets
        st.success("APIキー設定済み ✓")
    else:
        api_key = st.text_input(
            "Anthropic APIキー",
            type="password",
            placeholder="sk-ant-...",
            help="https://console.anthropic.com/ で取得",
        )
        if api_key:
            st.success("設定済み ✓")
        st.caption("アプリを閉じると消去されます")
    st.divider()
    st.caption("💰 料金目安: 1分析 約0.1〜0.3円")

tab_analyze, tab_history = st.tabs(["📸 分析する", "📈 上達記録"])

# ====== 分析タブ ======
with tab_analyze:
    sessions = load_sessions()
    session_num = len(sessions) + 1
    st.markdown(f"**レッスン #{session_num}**")

    uploaded = st.file_uploader(
        "レッスン後の写真をアップロード",
        type=["jpg", "jpeg", "png"],
        label_visibility="collapsed",
    )

    if uploaded:
        image_bytes = uploaded.read()

        # 画像リサイズ（API転送量削減）
        img = Image.open(io.BytesIO(image_bytes))
        if max(img.size) > 1920:
            img.thumbnail((1920, 1920), Image.LANCZOS)
            buf = io.BytesIO()
            fmt = "PNG" if uploaded.name.lower().endswith(".png") else "JPEG"
            img.save(buf, format=fmt, quality=85)
            image_bytes = buf.getvalue()

        mime_type = "image/png" if uploaded.name.lower().endswith(".png") else "image/jpeg"
        ext = "png" if uploaded.name.lower().endswith(".png") else "jpg"

        st.image(image_bytes, use_container_width=True)

        if not api_key:
            st.warning("👈 サイドバーでAPIキーを設定してください")
        else:
            if st.button("🔍 分析する", type="primary", use_container_width=True):
                with st.spinner(f"レッスン#{session_num}を分析中... 10〜20秒かかります"):
                    try:
                        result = analyze_image(image_bytes, api_key, sessions, mime_type)

                        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                        image_filename = save_image(image_bytes, session_id, ext)

                        save_session({
                            "id": session_id,
                            "date": datetime.now().isoformat(),
                            "session_number": session_num,
                            "image_filename": image_filename,
                            **result,
                        })

                        st.success(f"レッスン#{session_num}の分析完了！")
                        st.divider()
                        show_result(result, image_bytes, session_num)

                    except anthropic.AuthenticationError:
                        st.error("APIキーが無効です。正しいキーを確認してください。")
                    except anthropic.APIConnectionError:
                        st.error("ネットワークエラーです。接続を確認してください。")
                    except Exception as e:
                        st.error(f"エラー: {e}")
    else:
        st.markdown("""
<div style="border:2px dashed #C8B89A; border-radius:12px; padding:48px;
            text-align:center; color:#A0907A; background:white;">
    📷 ここに写真をドロップ<br>
    <small>または上の「Browse files」をクリック</small>
</div>
""", unsafe_allow_html=True)

# ====== 上達記録タブ ======
with tab_history:
    sessions = load_sessions()

    if not sessions:
        st.info("まだ記録がありません。「📸 分析する」タブから最初の写真を分析してみましょう。")
    else:
        # スコア推移グラフ（2回以上から）
        if len(sessions) >= 2:
            st.markdown("#### 📊 スコア推移")
            chart_data = pd.DataFrame([
                {
                    "レッスン": f"#{s['session_number']}",
                    "総合": s.get("overall_score", 0),
                    "形態": s.get("form_score", 0),
                    "色彩": s.get("color_score", 0),
                    "空間": s.get("space_score", 0),
                }
                for s in sessions
            ]).set_index("レッスン")
            st.line_chart(chart_data)

        # 繰り返し課題サマリー（2回以上出たもの）
        all_recurring = []
        for s in sessions:
            all_recurring.extend(s.get("recurring_issues", []))
        recurring_counts = Counter(all_recurring)
        persistent = [issue for issue, cnt in recurring_counts.items() if cnt >= 2]

        if persistent:
            st.markdown("#### ⚠️ 改善が必要な癖（複数回指摘あり）")
            for issue in persistent:
                st.warning(f"🔁 {issue}")

        # セッション一覧
        st.markdown("#### 📋 レッスン一覧")
        for s in reversed(sessions):
            date_str = datetime.fromisoformat(s["date"]).strftime("%Y/%m/%d")
            label = f"レッスン#{s['session_number']}　({date_str})　総合 {s.get('overall_score','?')}点"

            with st.expander(label):
                img_src = get_image_src(s)
                if img_src:
                    st.image(img_src, use_container_width=True)

                materials = s.get("flower_materials", [])
                if materials:
                    st.caption(f"花材: {'　・　'.join(materials)}")

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("総合", f"{s.get('overall_score', 0)}点")
                col2.metric("形態", f"{s.get('form_score', 0)}点")
                col3.metric("色彩", f"{s.get('color_score', 0)}点")
                col4.metric("空間", f"{s.get('space_score', 0)}点")

                issues = s.get("issues", [])
                if issues:
                    st.markdown("**課題:**")
                    for issue in sorted(issues, key=lambda x: x.get("priority", 99)):
                        st.markdown(
                            f"{'🔴' if issue.get('priority')==1 else '🟡'} "
                            f"**{issue['title']}** — {issue.get('fix','')}"
                        )

                focus = s.get("next_lesson_focus", "")
                if focus:
                    st.info(f"🎯 {focus}")
