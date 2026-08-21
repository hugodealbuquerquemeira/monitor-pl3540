#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor do PL 3540/2026 (CSLL/IRPJ de resseguradoras locais).

Consulta os dados abertos do Senado Federal e da Camara dos Deputados,
compara com um baseline salvo em disco e, quando aparece algo novo,
dispara mensagem no WhatsApp via CallMeBot.

Somente biblioteca padrao (sem pip install).

Variaveis de ambiente:
  WHATSAPP_PHONE     obrigatorio p/ enviar (ex.: +5511999999999)
  CALLMEBOT_APIKEY   obrigatorio p/ enviar
  STATE_FILE         default: state/pl3540_state.json
  SENADO_CODIGO      default: 175500  (codigo da materia no Senado)
  CAMARA_SIGLA/NUMERO/ANO  default: PL / 3540 / 2026
  DRY_RUN=1          nao envia nada, so imprime o que enviaria
  FORCE_NOTIFY=1     envia um resumo do estado atual mesmo sem novidade
  FIXTURE_SENADO / FIXTURE_CAMARA_LISTA / FIXTURE_CAMARA_TRAM
                     caminhos de arquivos locais, para teste offline

Saida: exit 0 em sucesso (com ou sem novidade). exit 1 se falhou ao
enviar (o baseline NAO e gravado, para tentar de novo na proxima rodada).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------- config

SENADO_CODIGO = os.environ.get("SENADO_CODIGO", "175500").strip()
CAMARA_SIGLA = os.environ.get("CAMARA_SIGLA", "PL").strip()
CAMARA_NUMERO = os.environ.get("CAMARA_NUMERO", "3540").strip()
CAMARA_ANO = os.environ.get("CAMARA_ANO", "2026").strip()

STATE_FILE = os.environ.get("STATE_FILE", "state/pl3540_state.json")
DRY_RUN = os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false", "False")
FORCE_NOTIFY = os.environ.get("FORCE_NOTIFY", "").strip() not in ("", "0", "false", "False")

SENADO_URL = "https://legis.senado.leg.br/dadosabertos/materia/movimentacoes/%s" % SENADO_CODIGO
SENADO_LINK = "https://www25.senado.leg.br/web/atividade/materias/-/materia/%s" % SENADO_CODIGO
CAMARA_API = "https://dadosabertos.camara.leg.br/api/v2"

TIMEOUT = 45
USER_AGENT = "monitor-pl3540/1.0 (+github actions; monitoramento legislativo)"
MAX_MSG_CHARS = 850          # CallMeBot corta mensagens muito longas
MAX_ITENS_MSG = 6            # quantos eventos novos detalhar por rodada

# Tags do XML do Senado que mudam sozinhas e nao sao noticia.
RUIDO_SENADO = (
    "Metadados",
    "DataVersaoServico",
    "VersaoServico",
    "DataUltimaAtualizacao",
    "DataHoraGeracao",
)


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------------ http

def http_get(url, accept):
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def http_get_retry(url, accept, tentativas=3):
    erro = None
    for i in range(tentativas):
        try:
            return http_get(url, accept)
        except Exception as exc:                      # noqa: BLE001
            erro = exc
            espera = 5 * (i + 1)
            log("  aviso: falha em %s (%s). nova tentativa em %ss" % (url, exc, espera))
            time.sleep(espera)
    raise RuntimeError("nao consegui buscar %s: %s" % (url, erro))


def ler_fixture(var):
    caminho = os.environ.get(var)
    if not caminho:
        return None
    with open(caminho, "rb") as fh:
        return fh.read()


# ---------------------------------------------------------------- senado

def _tag(elem):
    """Nome da tag sem namespace."""
    t = elem.tag
    return t.split("}", 1)[1] if "}" in t else t


def _folhas(elem, caminho=""):
    """Percorre a arvore e devolve (caminho, texto) de cada folha com texto."""
    nome = _tag(elem)
    atual = "%s/%s" % (caminho, nome) if caminho else nome
    filhos = list(elem)
    if not filhos:
        texto = (elem.text or "").strip()
        if texto:
            yield atual, texto
        return
    for filho in filhos:
        for sub in _folhas(filho, atual):
            yield sub


def coletar_senado():
    """Devolve (lista_de_fatos, rotulo). Cada fato e uma string estavel."""
    bruto = ler_fixture("FIXTURE_SENADO")
    if bruto is None:
        bruto = http_get_retry(SENADO_URL, "application/xml")
    raiz = ET.fromstring(bruto)

    fatos = []
    for caminho, texto in _folhas(raiz):
        if any(r in caminho for r in RUIDO_SENADO):
            continue
        fatos.append("%s :: %s" % (caminho.split("/")[-1], texto))
    # dedup preservando ordem
    fatos = list(dict.fromkeys(fatos))
    return fatos


# Campos do Senado em ordem de utilidade para um alerta legivel.
PRIORIDADE_SENADO = (
    ("Descricao", "Ementa", "Texto", "Despacho"),          # frases inteiras
    ("Situacao", "Local", "Origem", "Destino", "Data"),    # contexto seco
    ("Tramitando", "Numero", "Sigla"),                     # ultimo recurso
)


def humanizar_senado(fatos):
    """Escolhe, entre os fatos novos, os que valem virar texto de alerta.

    Usa o primeiro nivel de prioridade que tiver conteudo, para a mensagem
    nao virar uma sopa de siglas quando existe uma frase descritiva.
    """
    for nivel in PRIORIDADE_SENADO:
        saida = []
        for f in fatos:
            campo, _, valor = f.partition(" :: ")
            if any(k in campo for k in nivel):
                saida.append(valor)
        saida = [x for x in dict.fromkeys(saida) if x]
        if saida:
            return saida
    return []


# ---------------------------------------------------------------- camara

def coletar_camara():
    """Devolve lista de fatos da tramitacao na Camara (ou [] se indisponivel)."""
    bruto = ler_fixture("FIXTURE_CAMARA_LISTA")
    if bruto is None:
        url = "%s/proposicoes?%s" % (CAMARA_API, urllib.parse.urlencode({
            "siglaTipo": CAMARA_SIGLA,
            "numero": CAMARA_NUMERO,
            "ano": CAMARA_ANO,
        }))
        bruto = http_get_retry(url, "application/json")
    dados = json.loads(bruto.decode("utf-8"))
    itens = dados.get("dados") or []
    if not itens:
        log("  camara: proposicao nao encontrada")
        return [], None
    prop_id = itens[0].get("id")

    bruto2 = ler_fixture("FIXTURE_CAMARA_TRAM")
    if bruto2 is None:
        url2 = "%s/proposicoes/%s/tramitacoes" % (CAMARA_API, prop_id)
        bruto2 = http_get_retry(url2, "application/json")
    tram = json.loads(bruto2.decode("utf-8")).get("dados") or []

    fatos = []
    for t in tram:
        partes = [
            (t.get("dataHora") or "")[:16].replace("T", " "),
            t.get("siglaOrgao") or "",
            t.get("descricaoTramitacao") or "",
            (t.get("despacho") or "").strip(),
        ]
        fatos.append(" | ".join(p for p in partes if p))
    return list(dict.fromkeys(fatos)), prop_id


# ----------------------------------------------------------------- estado

def carregar_estado():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:                          # noqa: BLE001
        log("aviso: baseline ilegivel (%s). tratando como primeira rodada." % exc)
        return None


def salvar_estado(estado):
    pasta = os.path.dirname(STATE_FILE)
    if pasta:
        os.makedirs(pasta, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(estado, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------- callmebot

def enviar_whatsapp(texto):
    fone = os.environ.get("WHATSAPP_PHONE", "").strip()
    chave = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    if DRY_RUN or not fone or not chave:
        motivo = "DRY_RUN" if DRY_RUN else "WHATSAPP_PHONE/CALLMEBOT_APIKEY ausentes"
        log("--- nao enviado (%s). mensagem seria:\n%s\n---" % (motivo, texto))
        return DRY_RUN  # em dry-run consideramos sucesso
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode({
        "phone": fone,
        "text": texto,
        "apikey": chave,
    })
    try:
        resp = http_get(url, "text/plain")
        log("  callmebot ok (%d bytes de resposta)" % len(resp))
        return True
    except urllib.error.HTTPError as exc:
        log("  ERRO callmebot: HTTP %s %s" % (exc.code, exc.reason))
    except Exception as exc:                          # noqa: BLE001
        log("  ERRO callmebot: %s" % exc)
    return False


def partir(texto, limite=MAX_MSG_CHARS):
    """Quebra em pedacos respeitando linhas."""
    pedacos, atual = [], ""
    for linha in texto.split("\n"):
        while len(linha) > limite:
            pedacos.append(linha[:limite])
            linha = linha[limite:]
        if len(atual) + len(linha) + 1 > limite:
            if atual:
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
    for i, pedaco in enumerate(pedacos):
        if len(pedacos) > 1:
            pedaco = "(%d/%d) %s" % (i + 1, len(pedacos), pedaco)
        if not enviar_whatsapp(pedaco):
            ok = False
        if i < len(pedacos) - 1:
            time.sleep(5)     # CallMeBot nao gosta de rajada
    return ok


# ------------------------------------------------------------------- main

def main():
    log("== monitor PL %s/%s | baseline: %s" % (CAMARA_NUMERO, CAMARA_ANO, STATE_FILE))

    erros = []

    try:
        fatos_senado = coletar_senado()
        log("  senado: %d campos lidos" % len(fatos_senado))
    except Exception as exc:                          # noqa: BLE001
        erros.append("Senado: %s" % exc)
        fatos_senado = None

    try:
        fatos_camara, prop_id = coletar_camara()
        log("  camara: %d tramitacoes lidas" % len(fatos_camara))
    except Exception as exc:                          # noqa: BLE001
        erros.append("Camara: %s" % exc)
        fatos_camara, prop_id = None, None

    if fatos_senado is None and fatos_camara is None:
        log("ERRO: nenhuma das duas fontes respondeu. Nada a fazer.")
        for e in erros:
            log("  " + e)
        return 1

    anterior = carregar_estado()
    primeira_rodada = anterior is None

    novo_estado = {
        "senado": fatos_senado if fatos_senado is not None
        else (anterior or {}).get("senado", []),
        "camara": fatos_camara if fatos_camara is not None
        else (anterior or {}).get("camara", []),
        "camara_id": prop_id or (anterior or {}).get("camara_id"),
    }

    if primeira_rodada:
        salvar_estado(novo_estado)
        log("PRIMEIRA RODADA: baseline gravado, nada enviado (comportamento esperado).")
        if erros:
            log("  (com avisos: %s)" % "; ".join(erros))
        return 0

    novos_senado = [f for f in novo_estado["senado"] if f not in set(anterior.get("senado", []))]
    novos_camara = [f for f in novo_estado["camara"] if f not in set(anterior.get("camara", []))]

    if not novos_senado and not novos_camara and not FORCE_NOTIFY:
        log("Sem novidade. Nada enviado.")
        salvar_estado(novo_estado)   # absorve mudancas de ruido
        return 0

    houve_novidade = bool(novos_senado or novos_camara)
    titulo = "novidade na tramitacao" if houve_novidade else "teste manual (sem novidade)"
    linhas = ["*PL %s/%s* - %s" % (CAMARA_NUMERO, CAMARA_ANO, titulo)]

    if novos_camara:
        linhas.append("")
        linhas.append("[Camara]")
        for f in novos_camara[:MAX_ITENS_MSG]:
            linhas.append("- " + f)
        if len(novos_camara) > MAX_ITENS_MSG:
            linhas.append("- (+%d outros)" % (len(novos_camara) - MAX_ITENS_MSG))

    if novos_senado:
        destaques = humanizar_senado(novos_senado)
        linhas.append("")
        linhas.append("[Senado]")
        if destaques:
            for d in destaques[:MAX_ITENS_MSG]:
                linhas.append("- " + d)
            if len(destaques) > MAX_ITENS_MSG:
                linhas.append("- (+%d outros)" % (len(destaques) - MAX_ITENS_MSG))
        else:
            linhas.append("- %d campos mudaram (sem texto descritivo)" % len(novos_senado))

    if not houve_novidade:
        linhas.append("")
        linhas.append("Canal funcionando. Nenhuma movimentacao nova desde a ultima checagem.")

    linhas.append("")
    linhas.append(SENADO_LINK)
    if novo_estado.get("camara_id"):
        linhas.append("https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao=%s"
                      % novo_estado["camara_id"])
    if erros:
        linhas.append("(fonte indisponivel nesta rodada: %s)" % "; ".join(e.split(":")[0] for e in erros))

    mensagem = "\n".join(linhas)
    log("Novidade detectada (%d Senado / %d Camara)." % (len(novos_senado), len(novos_camara)))

    if notificar(mensagem):
        salvar_estado(novo_estado)
        log("Enviado e baseline atualizado.")
        return 0

    log("FALHA no envio. Baseline NAO atualizado - tenta de novo na proxima rodada.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
