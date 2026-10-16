#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor SUSEP/SES - lucro mensal do IRB Brasil Re (IRBR3).

O que faz: consulta o SES da SUSEP (Resseguradoras Locais -> Demonstracoes
Contabeis -> DRE, empresa FIP 31623 = IRB BRASIL RESSEGUROS S/A) e avisa no
WhatsApp quando o mes-alvo (MES_ALVO, ex. 202608) aparece.

A DRE do SES e ACUMULADA NO ANO. O resultado do mes e:
    mes = acumulado(MES_ALVO) - acumulado(MES_ALVO - 1)
(em janeiro o acumulado ja e o proprio mes).

A consulta do SES e um formulario ASP.NET: GET na pagina (pega __VIEWSTATE e
__EVENTVALIDATION + cookie de sessao), POST com empresa/demonstracao/mes, e o
servidor redireciona para resp_balanco.aspx, que so devolve a tabela se o
cookie de sessao vier junto. Por isso o opener com CookieJar.

Somente biblioteca padrao (sem pip install).

Variaveis de ambiente:
  WHATSAPP_PHONE, CALLMEBOT_APIKEY   obrigatorios p/ enviar
  MES_ALVO          default 202608 (AAAAMM)
  SUSEP_FIP         default 31623 (IRB)
  STATE_FILE        default state/susep_irb_state.json
  INICIO            AAAA-MM-DD: antes dessa data (BRT) a rodada agendada nao faz nada
  HEARTBEAT=1       manda mensagem em toda rodada enquanto o mes nao sai
  FORCE_NOTIFY=1    manda o resumo mesmo se ja notificado (teste do canal)
  DRY_RUN=1         nao envia, so imprime
  HTTP_TIMEOUT / HTTP_TENTATIVAS
  FIXTURE_FORM / FIXTURE_ALVO / FIXTURE_BASE / FIXTURE_ANTERIOR  teste offline

Saida: exit 0 em sucesso. exit 1 se falhou ao enviar o alerta (o estado NAO e
gravado, entao o alerta e reenviado na rodada seguinte).
"""

import datetime
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- config

def _flag(nome):
    return os.environ.get(nome, "").strip() not in ("", "0", "false", "False")

MES_ALVO = os.environ.get("MES_ALVO", "202608").strip()
SUSEP_FIP = os.environ.get("SUSEP_FIP", "31623").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state/susep_irb_state.json")
INICIO = os.environ.get("INICIO", "").strip()
HEARTBEAT = _flag("HEARTBEAT")
FORCE_NOTIFY = _flag("FORCE_NOTIFY")
DRY_RUN = _flag("DRY_RUN")
# TESTE: lucro ficticio do MES_ALVO em R$ mi. Simula a DRE do mes a partir do
# mes base, manda o alerta marcado como TESTE e NAO grava estado.
TESTE_LL_MES = os.environ.get("TESTE_LL_MES", "").strip().replace(",", ".")
# Consenso de lucro liquido do trimestre do MES_ALVO, em R$ mi (vazio = sem comparacao)
CONSENSO_TRI_MI = os.environ.get("CONSENSO_TRI_MI", "").strip().replace(",", ".")
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
TENTATIVAS_HTTP = int(os.environ.get("HTTP_TENTATIVAS", "4"))

SES_BASE = "https://www2.susep.gov.br/menuestatistica/ses/"
SES_FORM = SES_BASE + "balanco.aspx?tipo=resseglocal&id=55"
SES_LINK = SES_BASE + "principal.aspx"
DEMONSTRACAO_DRE = "23"
CAMPO = "ctl00$ContentPlaceHolder1$"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_MSG_CHARS = 850

# Linhas da DRE que entram no alerta, na ordem. (rotulo curto, prefixo da conta
# no SES em maiusculas, sem acento). Casa a PRIMEIRA ocorrencia.
LINHAS = (
    ("Premios emitidos", "PREMIOS EMITIDOS"),
    ("Premios ganhos", "(=) PREMIOS GANHOS"),
    ("Sinistros ocorridos", "(-) SINISTROS OCORRIDOS"),
    ("Resultado retrocessao", "(+) RESULTADO COM RETROCESSAO"),
    ("Custos aquisicao", "(-) CUSTOS DE AQUISICAO"),
    ("Desp. administrativas", "(-) DESPESAS ADMINISTRATIVAS"),
    ("Desp. tributos", "(-) DESPESAS COM TRIBUTOS"),
    ("Resultado financeiro", "(+) RESULTADO FINANCEIRO"),
    ("Resultado patrimonial", "(+) RESULTADO PATRIMONIAL"),
    ("Resultado operacional", "(=) RESULTADO OPERACIONAL"),
    ("LAIR", "(=) RESULTADO ANTES DOS IMPOSTOS"),
    ("IR", "(-) IMPOSTO DE RENDA"),
    ("CSLL", "(-) CONTRIBUICAO SOCIAL"),
    ("Participacoes", "(-) PARTICIPACOES SOBRE O RESULTADO"),
)
LL = "(=) LUCRO LIQUIDO / PREJUIZO"


def log(msg):
    print(msg, flush=True)


def agora_brt():
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=3)).replace(tzinfo=None)

# ------------------------------------------------------------------ meses

def mes_anterior(aaaamm):
    a, m = int(aaaamm[:4]), int(aaaamm[4:])
    return "%04d12" % (a - 1) if m == 1 else "%04d%02d" % (a, m - 1)


def rotulo_mes(aaaamm):
    return "%s/%s" % (aaaamm[4:], aaaamm[:4])

# ------------------------------------------------------------------- http

def novo_opener():
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", USER_AGENT),
                     ("Accept-Language", "pt-BR,pt;q=0.9")]
    return op


def ler_fixture(var):
    caminho = os.environ.get(var)
    if not caminho:
        return None
    with open(caminho, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def _decodifica(bruto):
    for enc in ("utf-8", "latin-1"):
        try:
            return bruto.decode(enc)
        except UnicodeDecodeError:
            continue
    return bruto.decode("utf-8", "replace")

# --------------------------------------------------------------- SES/DRE

def _hidden_fields(pagina):
    campos = {}
    for tag in re.findall(r"<input[^>]+>", pagina, re.I):
        if not re.search(r'type\s*=\s*"hidden"', tag, re.I):
            continue
        nome = re.search(r'name\s*=\s*"([^"]*)"', tag)
        valor = re.search(r'value\s*=\s*"([^"]*)"', tag)
        if nome:
            campos[nome.group(1)] = html.unescape(valor.group(1)) if valor else ""
    return campos


def _valor_empresa(pagina):
    """O value da option vem com espacos a direita ('31623     '), e o
    __EVENTVALIDATION exige o valor EXATO. Le da propria pagina."""
    for val in re.findall(r'<option[^>]*value\s*=\s*"([^"]*)"', pagina, re.I):
        if val.strip() == SUSEP_FIP:
            return val
    raise RuntimeError("empresa FIP %s nao aparece na lista do SES" % SUSEP_FIP)


def _sem_acento(txt):
    trocas = str.maketrans("ÁÀÂÃÉÊÍÓÔÕÚÜÇáàâãéêíóôõúüç", "AAAAEEIOOOUUCaaaaeeiooouuc")
    return txt.translate(trocas)


def _num(txt):
    txt = txt.strip().replace(".", "").replace(",", ".")
    if txt in ("", "-"):
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def parse_dre(pagina):
    """Lista ordenada de (conta_normalizada, valor). [] se o mes nao existe."""
    linhas = []
    for linha in re.findall(r"<tr[^>]*>(.*?)</tr>", pagina, re.S | re.I):
        cels = re.findall(r"<td[^>]*>(.*?)</td>", linha, re.S | re.I)
        if len(cels) != 2:
            continue
        conta = html.unescape(re.sub(r"<[^>]+>", " ", cels[0]))
        conta = _sem_acento(" ".join(conta.split())).upper()
        valor = _num(html.unescape(re.sub(r"<[^>]+>", "", cels[1])))
        if conta and valor is not None:
            linhas.append((conta, valor))
    return linhas


def buscar_dre(mes, fixture_var=None):
    fx = ler_fixture(fixture_var) if fixture_var else None
    pasta = os.environ.get("FIXTURE_DIR")
    if fx is None and pasta:
        caminho = os.path.join(pasta, "dre_%s.html" % mes)
        fx = open(caminho, encoding="utf-8").read() if os.path.exists(caminho) else ""
    if fx is not None:
        return parse_dre(fx)
    erro = None
    for i in range(TENTATIVAS_HTTP):
        try:
            op = novo_opener()   # sessao nova a cada tentativa
            form = ler_fixture("FIXTURE_FORM")
            if form is None:
                form = _decodifica(op.open(SES_FORM, timeout=TIMEOUT).read())
            dados = _hidden_fields(form)
            if "__VIEWSTATE" not in dados:
                raise RuntimeError("formulario do SES sem __VIEWSTATE (layout mudou?)")
            dados[CAMPO + "edEmpresas"] = _valor_empresa(form)
            dados[CAMPO + "edDemonstracao"] = DEMONSTRACAO_DRE
            dados[CAMPO + "edMes"] = mes
            dados[CAMPO + "Button1"] = "Processar"
            corpo = urllib.parse.urlencode(dados).encode("ascii")
            req = urllib.request.Request(SES_FORM, data=corpo, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": SES_FORM,
                "Origin": "https://www2.susep.gov.br",
            })
            resp = op.open(req, timeout=TIMEOUT)
            pagina = _decodifica(resp.read())
            if "resp_balanco" not in resp.geturl():
                raise RuntimeError("POST nao redirecionou para resp_balanco (HTTP %s)"
                                   % resp.status)
            if "Empresas selecionadas" not in pagina:
                raise RuntimeError("pagina de resposta inesperada")
            return parse_dre(pagina)
        except Exception as exc:  # noqa: BLE001
            erro = exc
            espera = 10 * (i + 1)
            log("  aviso: SES %s falhou (%s). nova tentativa em %ss" % (mes, exc, espera))
            time.sleep(espera)
    raise RuntimeError("SES indisponivel para %s: %s" % (mes, erro))


def pega(dre, prefixo):
    for conta, valor in dre:
        if conta.startswith(prefixo):
            return valor
    return None

# ----------------------------------------------------------------- estado

def carregar_estado():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log("aviso: estado ilegivel (%s). comecando do zero." % exc)
        return {}


def salvar_estado(estado):
    pasta = os.path.dirname(STATE_FILE)
    if pasta:
        os.makedirs(pasta, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(estado, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, STATE_FILE)

# -------------------------------------------------------------- callmebot

def enviar_whatsapp(texto):
    fone = os.environ.get("WHATSAPP_PHONE", "").strip()
    chave = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    if DRY_RUN or not fone or not chave:
        motivo = "DRY_RUN" if DRY_RUN else "WHATSAPP_PHONE/CALLMEBOT_APIKEY ausentes"
        log("--- nao enviado (%s). mensagem seria:\n%s\n---" % (motivo, texto))
        return DRY_RUN
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode({
        "phone": fone, "text": texto, "apikey": chave})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            corpo = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        log("  ERRO callmebot: HTTP %s %s" % (exc.code, exc.reason))
        return False
    except Exception as exc:  # noqa: BLE001
        log("  ERRO callmebot: %s" % exc)
        return False
    # Repo publico = log publico. Mascara digitos (telefone/apikey).
    limpo = " ".join(re.sub(r"<[^>]+>", " ", corpo).split())
    limpo = "".join("x" if ch.isdigit() else ch for ch in limpo)
    # O fim da resposta e onde o CallMeBot diz se enfileirou ou se a apikey caiu.
    log("  callmebot HTTP 200, fim da resposta: ...%s" % limpo[-300:])
    baixo = limpo.lower()
    if "not sent" in baixo or "messages left" in baixo and "subscribe" in baixo:
        log("  ERRO callmebot: mensagem NAO enviada (cota/assinatura do CallMeBot)")
        return False
    if ("apikey" in baixo and ("invalid" in baixo or "not valid" in baixo)) \
            or "reactivat" in baixo or "activate" in baixo and "you need" in baixo:
        log("  ERRO callmebot: apikey invalida/expirada - reative o bot no WhatsApp")
        return False
    return True


def partir(texto, limite=MAX_MSG_CHARS):
    pedacos, atual = [], ""
    for linha in texto.split("\n"):
        if len(atual) + len(linha) + 1 > limite and atual:
            pedacos.append(atual)
            atual = linha
        else:
            atual = (atual + "\n" + linha) if atual else linha
    if atual:
        pedacos.append(atual)
    return pedacos


def notificar(texto):
    pedacos = partir(texto)
    ok = True
    for i, p in enumerate(pedacos):
        if len(pedacos) > 1:
            p = "(%d/%d) %s" % (i + 1, len(pedacos), p)
        if not enviar_whatsapp(p):
            ok = False
        if i < len(pedacos) - 1:
            time.sleep(5)
    return ok

# ---------------------------------------------------------------- formato

def mi(v):
    if v is None:
        return "n/d"
    s = "%.1f" % (v / 1e6)
    return "R$ " + s.replace(".", ",") + " mi"


def montar_alerta(alvo, base, anterior, mes_base, titulo):
    janeiro = MES_ALVO.endswith("01")
    ll_alvo, ll_base = pega(alvo, LL), (0.0 if janeiro else pega(base, LL))
    ll_mes = None if ll_alvo is None or ll_base is None else ll_alvo - ll_base
    ll_mes_ant = None
    if not janeiro and anterior is not None:
        ant_base = 0.0 if mes_base.endswith("01") else pega(anterior, LL)
        if ll_base is not None and ant_base is not None:
            ll_mes_ant = ll_base - ant_base

    linhas = [
        "*IRB (IRBR3) - SUSEP %s* - %s" % (rotulo_mes(MES_ALVO), titulo),
        "",
        "*Lucro liquido %s: %s*" % (rotulo_mes(MES_ALVO), mi(ll_mes)),
        "(acum. %s %s - acum. %s %s)" % (
            rotulo_mes(MES_ALVO), mi(ll_alvo),
            rotulo_mes(mes_base), "-" if janeiro else mi(ll_base)),
    ]
    if ll_mes_ant is not None:
        linhas.append("Mes anterior (%s): %s" % (rotulo_mes(mes_base), mi(ll_mes_ant)))
    linhas += ["", "[DRE do mes = acum. %s - acum. %s]" % (MES_ALVO[4:], mes_base[4:])]
    for rot, pref in LINHAS:
        a = pega(alvo, pref)
        b = 0.0 if janeiro else pega(base, pref)
        if a is None or b is None:
            continue
        linhas.append("- %s: %s" % (rot, mi(a - b)))
    return "\n".join(linhas), ll_mes, ll_alvo

# ------------------------------------------------- trimestre x consenso

def bloco_trimestre(serie):
    """Soma dos meses ja publicados do trimestre do MES_ALVO, implicito para o
    trimestre ((soma / n meses) * 3) e miss/beat contra o consenso."""
    ano, m = MES_ALVO[:4], int(MES_ALVO[4:])
    tri = (m - 1) // 3 + 1
    meses = ["%s%02d" % (ano, x) for x in range(3 * tri - 2, m + 1)]
    valores = {mes: v for mes, v, _ in serie}
    if any(valores.get(x) is None for x in meses):
        return ""
    soma = sum(valores[x] for x in meses)
    n = len(meses)
    implicito = soma / n * 3
    nome_tri = "%dQ%s" % (tri, ano[2:])
    rot = " + ".join(rotulo_curto(x) for x in meses)
    linhas = ["", "*%s*" % nome_tri,
              "- Somado %s: %s" % (rot, mi(soma))]
    if n < 3:
        linhas.append("- Implicito %s ((%s)/%d x 3): *%s*" % (nome_tri, rot, n, mi(implicito)))
    else:
        linhas.append("- %s fechado: *%s*" % (nome_tri, mi(implicito)))
    if CONSENSO_TRI_MI:
        cons = float(CONSENSO_TRI_MI) * 1e6
        desvio = implicito / cons - 1
        tag = "MISS" if desvio < 0 else "BEAT"
        linhas.append("- Consenso %s: %s" % (nome_tri, mi(cons)))
        linhas.append("- *%s: %s%%* (implicito / consenso - 1)"
                      % (tag, ("%+.1f" % (desvio * 100)).replace(".", ",")))
    return "\n".join(linhas)

# ------------------------------------------------- serie mensal + graficos

MESES_PT = ("jan", "fev", "mar", "abr", "mai", "jun",
            "jul", "ago", "set", "out", "nov", "dez")


def rotulo_curto(aaaamm):
    return "%s-%s" % (MESES_PT[int(aaaamm[4:]) - 1], aaaamm[2:4])


def serie_mensal(ja_lidos):
    """[(AAAAMM, lucro do mes, lucro acumulado)] de janeiro ate MES_ALVO.
    Mes = acumulado(m) - acumulado(m-1); janeiro = acumulado de janeiro."""
    ano = MES_ALVO[:4]
    acum = {}
    for m in range(1, int(MES_ALVO[4:]) + 1):
        mes = "%s%02d" % (ano, m)
        dre = ja_lidos.get(mes)
        if dre is None:
            dre = buscar_dre(mes)
        acum[mes] = pega(dre, LL)
    serie, anterior = [], 0.0
    for mes in sorted(acum):
        a = acum[mes]
        serie.append((mes, None if a is None or anterior is None else a - anterior, a))
        anterior = a
    return serie


def grafico_texto(serie):
    """Barras em texto: o CallMeBot so manda texto no WhatsApp."""
    vals = [abs(v) for _, v, _ in serie if v is not None]
    escala = max(vals) if vals else 1.0
    linhas = ["```"]
    for mes, v, _ in serie:
        if v is None:
            linhas.append("%s  n/d" % rotulo_curto(mes))
            continue
        n = max(1, int(round(12 * abs(v) / escala))) if v else 0
        barra = ("█" if v >= 0 else "▒") * n
        linhas.append("%s %6.1f %s" % (rotulo_curto(mes), v / 1e6, barra))
    linhas.append("```")
    linhas.append("(R$ mi; ▒ = prejuizo)")
    return "\n".join(linhas)


def calc_trimestre(serie):
    """(nome_tri, n_meses, implicito, consenso|None, desvio|None) ou None."""
    ano, m = MES_ALVO[:4], int(MES_ALVO[4:])
    tri = (m - 1) // 3 + 1
    meses = ["%s%02d" % (ano, x) for x in range(3 * tri - 2, m + 1)]
    valores = {mes: v for mes, v, _ in serie}
    if any(valores.get(x) is None for x in meses):
        return None
    implicito = sum(valores[x] for x in meses) / len(meses) * 3
    cons = float(CONSENSO_TRI_MI) * 1e6 if CONSENSO_TRI_MI else None
    desvio = implicito / cons - 1 if cons else None
    return "%dQ%s" % (tri, ano[2:]), len(meses), implicito, cons, desvio


def grafico_png(serie, caminho):
    """PNG do lucro mensal + run-rate do trimestre x consenso.
    Instala matplotlib so quando precisa."""
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "matplotlib"],
                       check=True, timeout=240)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def br(v):
        return ("%.1f" % v).replace(".", ",")

    rot = [rotulo_curto(m) for m, _, _ in serie]
    vals = [(v or 0) / 1e6 for _, v, _ in serie]
    cores = ["#1f4e79" if v >= 0 else "#c0392b" for v in vals]
    cores[-1] = "#2e86c1" if vals[-1] >= 0 else "#e74c3c"
    xs = list(range(len(vals)))

    tri = calc_trimestre(serie)
    if tri:
        nome, n, impl, cons, desvio = tri
        x_rr = len(vals) + 0.6          # espaco separando mensal x trimestral
        xs.append(x_rr)
        rot.append("%s\nrun-rate" % nome if n < 3 else "%s\nrealizado" % nome)
        vals.append(impl / 1e6)
        cores.append("#7f8c8d")
        if cons:
            x_c = x_rr + 1
            xs.append(x_c)
            rot.append("%s\nconsenso" % nome)
            vals.append(cons / 1e6)
            cores.append("#d5d8dc")

    fig, ax = plt.subplots(figsize=(9.5, 5), dpi=150)
    barras = ax.bar(xs, vals, color=cores, width=0.8,
                    edgecolor=["none"] * (len(vals) - 1) + ["#95a5a6"])
    for b, v in zip(barras, vals):
        ax.annotate(br(v), (b.get_x() + b.get_width() / 2, v),
                    xytext=(0, 3 if v >= 0 else -11), textcoords="offset points",
                    ha="center", fontsize=9)
    ax.set_xticks(xs)
    ax.set_xticklabels(rot, fontsize=9)
    ax.axhline(0, color="#555", linewidth=0.8)

    if tri and cons:
        topo = max(vals) * 1.0
        ax.set_ylim(top=max(vals) * 1.28)
        # seta pontilhada vermelha: topo do run-rate -> topo do consenso
        pad = topo * 0.07               # sai/chega acima dos rotulos de valor
        ax.annotate("", xy=(x_c - 0.2, cons / 1e6 + pad), xytext=(x_rr, impl / 1e6 + pad),
                    arrowprops=dict(arrowstyle="-|>", color="#e74c3c", lw=1.8,
                                    linestyle=(0, (2, 2)), shrinkA=4, shrinkB=4,
                                    connectionstyle="arc3,rad=-0.25"))
        tag = "MISS" if desvio < 0 else "BEAT"
        ax.text((x_rr + x_c) / 2, max(impl, cons) / 1e6 + topo * 0.13,
                "%s %s%%" % (tag, ("%+.1f" % (desvio * 100)).replace(".", ",")),
                ha="center", va="bottom", fontsize=11, fontweight="bold",
                color="#e74c3c" if desvio < 0 else "#1e8449")
        ax.axvline(len(serie) - 0.2, color="#bbb", linewidth=0.8, linestyle=":")

    ax.set_title(("TESTE (%s ficticio) - " % rotulo_curto(MES_ALVO) if TESTE_LL_MES else "")
                 + "IRB Brasil Re - lucro liquido mensal (SUSEP/SES)", fontsize=12)
    ax.set_ylabel("R$ milhoes")
    ax.spines[["top", "right"]].set_visible(False)
    acum = serie[-1][2]
    if acum is not None:
        ax.text(0.01, 0.97, "Acumulado %s: R$ %s mi" % (
            rotulo_curto(serie[-1][0]), br(acum / 1e6)),
            transform=ax.transAxes, va="top", fontsize=9, color="#333")
    nota = "Mes = DRE acumulada do mes - acumulada do mes anterior. FIP %s." % SUSEP_FIP
    if tri and tri[1] < 3:
        nota = "Run-rate = media dos meses do trimestre x 3. " + nota
    fig.text(0.99, 0.01, nota, ha="right", fontsize=7, color="#777")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
    fig.savefig(caminho)
    plt.close(fig)


def publicar_png(caminho):
    """Commita o PNG ANTES de mandar o WhatsApp, para o link ja abrir.
    Devolve a URL ou None (fora do GitHub Actions ou se o push falhar)."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    ramo = os.environ.get("GITHUB_REF_NAME", "main")
    if not repo or DRY_RUN:
        return None
    import subprocess

    def git(*args):
        subprocess.run(["git"] + list(args), check=True, timeout=120)
    try:
        git("config", "user.name", "susep-irb-monitor[bot]")
        git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        git("add", caminho)
        sem_mudanca = subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0
        if sem_mudanca:   # mesmo PNG ja publicado: so devolve o link
            return "https://raw.githubusercontent.com/%s/%s/%s" % (repo, ramo, caminho)
        git("commit", "-m", "grafico: lucro mensal IRB %s [skip ci]" % MES_ALVO)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", ramo],
                       timeout=120)
        git("push", "origin", "HEAD:%s" % ramo)
    except Exception as exc:  # noqa: BLE001
        log("  aviso: nao publiquei o grafico (%s)" % exc)
        return None
    return "https://raw.githubusercontent.com/%s/%s/%s" % (repo, ramo, caminho)

# ------------------------------------------------------------------- main

def main():
    evento = os.environ.get("GITHUB_EVENT_NAME", "")
    hoje = agora_brt().date()
    if INICIO and evento == "schedule" and hoje.isoformat() < INICIO:
        log("Antes de INICIO=%s (hoje %s BRT). Nada a fazer." % (INICIO, hoje))
        return 0

    mes_base = mes_anterior(MES_ALVO)
    log("== monitor SUSEP IRB | alvo %s | base %s | estado %s"
        % (MES_ALVO, mes_base, STATE_FILE))
    estado = carregar_estado()
    if estado.get("mes_alvo") != MES_ALVO:
        estado = {"mes_alvo": MES_ALVO}

    try:
        alvo = buscar_dre(MES_ALVO, "FIXTURE_ALVO")
    except Exception as exc:  # noqa: BLE001
        log("ERRO: %s" % exc)
        if HEARTBEAT or FORCE_NOTIFY:
            enviar_whatsapp("*IRB SUSEP %s* - heartbeat: SES da SUSEP NAO respondeu "
                            "nesta rodada (%s). Tento de novo em 30 min.\n%s"
                            % (rotulo_mes(MES_ALVO), str(exc)[:160], SES_LINK))
        return 1
    log("  %s: %d linhas na DRE" % (MES_ALVO, len(alvo)))

    if TESTE_LL_MES:
        base_real = buscar_dre(mes_base, "FIXTURE_BASE")
        if not base_real or pega(base_real, LL) is None:
            log("TESTE: mes base %s sem dados, nao da para simular." % mes_base)
            return 1
        n = int(mes_base[4:])
        fator = (n + 1.0) / n          # mais um mes "medio" em cada linha
        ficticio = float(TESTE_LL_MES) * 1e6
        alvo = [(c, (pega(base_real, LL) + ficticio) if c.startswith(LL) else v * fator)
                for c, v in base_real]
        log("TESTE: DRE de %s SIMULADA, lucro do mes ficticio = R$ %s mi"
            % (MES_ALVO, TESTE_LL_MES))

    if not alvo or pega(alvo, LL) is None:
        log("Mes %s ainda NAO publicado no SES." % MES_ALVO)
        if HEARTBEAT or FORCE_NOTIFY:
            ref = estado.get("ll_base_acum")
            extra = ""
            if ref is None:
                try:
                    base = buscar_dre(mes_base, "FIXTURE_BASE")
                    ref = pega(base, LL)
                    if ref is not None:
                        estado["ll_base_acum"] = ref
                        salvar_estado(estado)
                except Exception as exc:  # noqa: BLE001
                    log("  aviso: nao li o mes base (%s)" % exc)
            if ref is not None:
                extra = "\nUltimo disponivel: LL acum. %s = %s" % (rotulo_mes(mes_base), mi(ref))
            msg = ("*IRB SUSEP %s* - heartbeat %s\nAinda nao publicado no SES. "
                   "Monitor e WhatsApp OK.%s"
                   % (rotulo_mes(MES_ALVO), agora_brt().strftime("%d/%m %H:%M"), extra))
            return 0 if enviar_whatsapp(msg) else 1
        return 0

    # Mes saiu.
    base = buscar_dre(mes_base, "FIXTURE_BASE") if not MES_ALVO.endswith("01") else []
    try:
        anterior = buscar_dre(mes_anterior(mes_base), "FIXTURE_ANTERIOR")
    except Exception as exc:  # noqa: BLE001
        log("  aviso: sem o mes anterior ao base (%s)" % exc)
        anterior = None

    assinatura = {"alvo": pega(alvo, LL), "base": pega(base, LL) if base else None}
    ja = estado.get("notificado")
    revisado = ja and estado.get("assinatura") != assinatura

    if TESTE_LL_MES:
        ja, revisado = False, False
    if ja and not revisado and not FORCE_NOTIFY and not TESTE_LL_MES:
        log("Ja notificado em %s e sem revisao de dados. Nada enviado." % estado.get("quando"))
        return 0

    titulo = "*SAIU*"
    if TESTE_LL_MES:
        titulo = "*TESTE - VALOR FICTICIO, NAO E DADO REAL*"
    if revisado:
        titulo = "*DADO REVISADO* pela SUSEP (recarga)"
    elif ja:
        titulo = "reenvio (teste)"
    msg, ll_mes, ll_acum = montar_alerta(alvo, base, anterior, mes_base, titulo)

    # Serie jan..alvo + graficos. Falha aqui nunca segura o alerta principal.
    extra = []
    try:
        lidos = {MES_ALVO: alvo, mes_base: base}
        if anterior is not None:
            lidos[mes_anterior(mes_base)] = anterior
        serie = serie_mensal(lidos)
        tri = bloco_trimestre(serie)
        if tri:
            msg = msg.replace("\n\n[DRE do mes", "\n" + tri + "\n\n[DRE do mes", 1)
        extra += ["", "*Lucro mensal %s a %s*" % (rotulo_curto(serie[0][0]),
                                                  rotulo_curto(serie[-1][0])),
                  grafico_texto(serie)]
        try:
            png = ("charts/TESTE_irb_lucro_mensal_%s.png" if TESTE_LL_MES
                   else "charts/irb_lucro_mensal_%s.png") % MES_ALVO
            grafico_png(serie, png)
            log("  grafico gravado em %s" % png)
            url = publicar_png(png)
            if url:
                extra += ["", "Grafico: " + url]
        except Exception as exc:  # noqa: BLE001
            log("  aviso: grafico PNG falhou (%s)" % exc)
        estado["serie"] = [[m, v, a] for m, v, a in serie]
    except Exception as exc:  # noqa: BLE001
        log("  aviso: serie mensal falhou (%s)" % exc)
    extra += ["", "Fonte: SES/SUSEP, Resseguradoras Locais > DRE, FIP %s" % SUSEP_FIP]
    msg_graf = "\n".join(extra).strip()
    log(msg)
    log(msg_graf)

    if notificar(msg):
        if msg_graf:
            time.sleep(5)
            if not notificar(("*TESTE* " if TESTE_LL_MES else "") + "*IRB (IRBR3)* - " + msg_graf):
                log("  aviso: mensagem do grafico nao foi enviada")
        if TESTE_LL_MES:
            log("TESTE enviado. Estado NAO gravado (o alerta real continua pendente).")
            return 0
        estado.update({"notificado": True, "assinatura": assinatura,
                       "ll_mes": ll_mes, "ll_acum": ll_acum,
                       "quando": agora_brt().strftime("%Y-%m-%d %H:%M BRT")})
        salvar_estado(estado)
        log("Enviado e estado gravado.")
        return 0
    log("FALHA no envio. Estado NAO gravado - reenvia na proxima rodada.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
