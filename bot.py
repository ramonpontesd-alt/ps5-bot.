"""Monitor de preço do PS5 no Mercado Livre -> avisa por Telegram e e-mail."""
import json
import os
import re
import smtplib
import statistics
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ---------- CONFIGURAÇÃO (ajuste aqui ou via variáveis no workflow) ----------
BUSCA = os.getenv("BUSCA", "playstation 5 console")
VERSAO = os.getenv("VERSAO", "")            # "", "digital" ou "disco"
TOLERANCIA = float(os.getenv("TOLERANCIA", "0"))   # 0 = só abaixo da média; 5 = até 5% acima
PRECO_MIN = float(os.getenv("PRECO_MIN", "2000"))  # descarta acessórios/jogos
PRECO_MAX = float(os.getenv("PRECO_MAX", "12000"))
REP_MINIMA = int(os.getenv("REP_MINIMA", "4"))     # 4 = verde claro ou melhor (1 a 5)
SOMENTE_SP = os.getenv("SOMENTE_SP", "false").lower() == "true"
PAGINAS = int(os.getenv("PAGINAS", "3"))           # 3 páginas x 50 anúncios
# -----------------------------------------------------------------------------

ML_ID = os.getenv("ML_CLIENT_ID", "")
ML_SECRET = os.getenv("ML_CLIENT_SECRET", "")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.getenv("EMAIL_TO", GMAIL_USER)

SEEN_FILE = Path("seen.json")
EXCLUIR = re.compile(
    r"usad|defeito|retirada|pe[cç]as|sucata|capa\b|controle|dualsense|jogo|"
    r"suporte|skin|headset|fone|cabo|base\b|ps4|playstation\s*4|\bvr\b|"
    r"cart[aã]o|gift|assinatura|pulse|portal|camera|c[aâ]mera",
    re.I,
)
INCLUIR = re.compile(r"\b(ps5|playstation\s*5)\b", re.I)


def token_ml():
    r = requests.post(
        "https://api.mercadolibre.com/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": ML_ID,
            "client_secret": ML_SECRET,
        },
        headers={"accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def buscar(tk):
    itens = []
    for off in range(0, PAGINAS * 50, 50):
        r = requests.get(
            "https://api.mercadolibre.com/sites/MLB/search",
            params={"q": BUSCA, "condition": "new", "limit": 50, "offset": off},
            headers={"Authorization": f"Bearer {tk}"},
            timeout=30,
        )
        if r.status_code == 403:
            raise RuntimeError(
                "Mercado Livre devolveu 403 (bloqueio de IP/credenciais). "
                "Veja a seção 'Problemas comuns' no LEIA-ME."
            )
        r.raise_for_status()
        res = r.json().get("results", [])
        if not res:
            break
        itens += res
    return itens


def reputacao(item):
    lvl = ((item.get("seller") or {}).get("seller_reputation") or {}).get("level_id")
    try:
        return int(str(lvl)[0])
    except (TypeError, ValueError):
        return 0


def filtrar(itens):
    ok = []
    for it in itens:
        t = it.get("title", "")
        p = it.get("price")
        if not p or not INCLUIR.search(t) or EXCLUIR.search(t):
            continue
        if not (PRECO_MIN <= p <= PRECO_MAX):
            continue
        if VERSAO == "digital" and "digital" not in t.lower():
            continue
        if VERSAO == "disco" and "digital" in t.lower():
            continue
        if SOMENTE_SP and (it.get("address") or {}).get("state_id") != "BR-SP":
            continue
        ok.append(it)
    return ok


def preco_medio(itens):
    precos = [i["price"] for i in itens]
    mediana = statistics.median(precos)
    sem_outliers = [p for p in precos if 0.5 * mediana <= p <= 1.5 * mediana]
    return statistics.mean(sem_outliers), mediana, len(sem_outliers)


def brl(v):
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def enviar_telegram(texto):
    if not (TG_TOKEN and TG_CHAT):
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": texto},
        timeout=30,
    )
    if not r.ok:
        print("Falha Telegram:", r.text)


def enviar_email(assunto, texto):
    if not (GMAIL_USER and GMAIL_PASS):
        return
    msg = MIMEText(texto, "plain", "utf-8")
    msg["Subject"] = assunto
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)
    except Exception as e:  # noqa: BLE001
        print("Falha e-mail:", e)


def main():
    if not (ML_ID and ML_SECRET):
        sys.exit("Faltam ML_CLIENT_ID / ML_CLIENT_SECRET nos Secrets.")

    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    itens = filtrar(buscar(token_ml()))
    print(f"{len(itens)} anúncios válidos após filtros")
    if len(itens) < 5:
        print("Amostra pequena demais para calcular preço médio. Encerrando.")
        return

    media, mediana, n = preco_medio(itens)
    limite = media * (1 + TOLERANCIA / 100)
    print(f"Preço médio: {brl(media)} | mediana: {brl(mediana)} | base: {n} anúncios")

    ofertas = []
    for it in sorted(itens, key=lambda i: i["price"]):
        p = it["price"]
        if p > limite or reputacao(it) < REP_MINIMA:
            continue
        antes = seen.get(it["id"])
        if antes is not None and p > antes * 0.95:  # já avisado, sem queda relevante
            continue
        seen[it["id"]] = p
        ofertas.append(it)

    if ofertas:
        linhas = [f"🎮 PS5 abaixo da média ({brl(media)})\n"]
        for it in ofertas[:8]:
            p = it["price"]
            dif = (1 - p / media) * 100
            alerta = "\n⚠️ Preço muito abaixo do normal: confira o vendedor (risco de golpe)" if p < 0.6 * media else ""
            linhas.append(
                f"• {it['title']}\n  {brl(p)} ({dif:+.0f}% vs média)"
                f"{alerta}\n  {it['permalink']}\n"
            )
        texto = "\n".join(linhas)
        enviar_telegram(texto)
        enviar_email(f"PS5 em oferta: a partir de {brl(ofertas[0]['price'])}", texto)
        print(f"{len(ofertas)} oferta(s) enviada(s)")
    else:
        print("Nenhuma oferta nova.")

    SEEN_FILE.write_text(json.dumps(seen, indent=1))


if __name__ == "__main__":
    main()
