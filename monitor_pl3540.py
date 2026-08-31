#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor do PL 3540/2026 (CSLL/IRPJ de resseguradoras locais).

Onde a materia esta: aprovada na Camara em regime de urgencia (art. 155
RICD) e autuada no Senado em 13/08/2026 como CASA REVISORA, situacao
AGUARDANDO DESPACHO. A acao agora e toda no Senado; a Camara so volta a
se mexer se o Senado emendar o texto.

Fontes consultadas, em ordem de importancia:

  1. Processo no Senado - /dadosabertos/processo/<idProcesso>
     Substitui /materia/movimentacoes/, que o proprio Senado marcou como
     depreciado (desativacao anunciada para 01/02/2026). E onde entram
     despacho, designacao de relator, prazo de emendas, parecer,
     deliberacao em comissao e inclusao em Ordem do Dia.

  2. Pauta do Plenario - /dadosabertos/plenario/agenda/mes/AAAAMMDD
     Mes corrente e proximo, casando por CodigoMateria. A pauta e
     publicada antes da sessao: e o aviso mais adiantado de que a
     materia vai a voto.

  3. Pauta das comissoes - /dadosabertos/agendareuniao/AAAAMMDD/AAAAMMDD
     Opcional (AGENDA_COMISSOES=1). Resposta grande (~1,7 MB), casa por
     idProcesso. So faz sentido depois de haver despacho para comissao.

  4. Tramitacao na Camara - dadosabertos.camara.leg.br

Somente biblioteca padrao (sem pip install).

Variaveis de ambiente:
  WHATSAPP_PHONE      obrigatorio p/ enviar (ex.: +5511999999999)
  CALLMEBOT_APIKEY    obrigatorio p/ enviar
  STATE_FILE          default: state/pl3540_state.json
  SENADO_PROCESSO     default: 9096233  (idProcesso no Senado)
  SENADO_CODIGO       default: 175500   (codigo da materia no Senado)
  CAMARA_SIGLA/NUMERO/ANO   default: PL / 3540 / 2026
  AGENDA_COMISSOES=1  liga a varredura das pautas de comissao
  HTTP_TIMEOUT        default: 20 (segundos por requisicao)
  HTTP_TENTATIVAS     default: 4
  DRY_RUN=1           nao envia nada, so imprime o que enviaria
  FORCE_NOTIFY=1      envia um resumo do estado atual mesmo sem novidade
  FIXTURE_SENADO / FIXTURE_AGENDA / FIXTURE_AGENDA_COMISSOES /
  FIXTURE_CAMARA_LISTA / FIXTURE_CAMARA_TRAM
                      caminhos de arquivos locais, para teste offline

Saida: exit 0 em sucesso (com ou sem novidade). exit 1 se falhou ao
enviar (o baseline NAO e gravado, para tentar de novo na proxima rodada).
"""

import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------- config

SENADO_PROCESSO = os.environ.get("SENADO_PROCESSO", "9096233").strip()
SENADO_CODIGO = os.environ.get("SENADO_CODIGO", "175500").strip()
CAMARA_SIGLA = os.environ.get("CAMARA_SIGLA", "PL").strip()
CAMARA_NUMERO = os.environ.get("CAMARA_NUMERO", "3540").strip()
CAMARA_ANO = os.environ.get("CAMARA_ANO", "2026").strip()

STATE_FILE = os.environ.get("STATE_FILE", "state/pl3540_state.json")
DRY_RUN = os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false", "False")
FORCE_NOTIFY = os.environ.get("FORCE_NOTIFY", "").strip() not in ("", "0", "false", "False")
AGENDA_COMISSOES = os.environ.get("AGENDA_COMISSOES", "").strip() not in ("", "0", "false", "False")

SENADO_API = "https://legis.senado.leg.br/dadosabertos"
SENADO_PROC_URL = "%s/processo/%s" % (SENADO_API, SENADO_PROCESSO)
SENADO_AGENDA_URL = "%s/plenario/agenda/mes/%%s" % SENADO_API
SENADO_REUNIOES_URL = "%s/agendareuniao/%%s/%%s" % SENADO_API
SENADO_LINK = "https://www25.senado.leg.br/web/atividade/materias/-/materia/%s" % SENADO_CODIGO
CAMARA_API = "https://dadosabertos.camara.leg.br/api/v2"

TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "20"))
TENTATIVAS_HTTP = int(os.environ.get("HTTP_TENTATIVAS", "4"))
USER_AGENT = "monitor-pl3540/2.0 (+github actions; monitoramento legislativo)"
MAX_MSG_CHARS = 850     # CallMeBot corta mensagens muito longas
MAX_ITENS_MSG = 6       # quantos eventos novos detalhar por rodada
SCHEMA = 2              # baseline gravado por versao anterior e descartado

# Campos do XML que mudam sozinhos e nao sao noticia.
RUIDO_SENADO = (
    "Metadados",
    "DataVersaoServico",
    "VersaoServico",
    "DescricaoDataSet",
    "Versao",
    "versao",
    "dthUltimaAtualizacao",
    "ultimaInformacaoAtualizada",
)

# Eventos que mudam a leitura do cronograma. Casados em minusculas.
EVENTOS_CHAVE = (
    ("despacho", "despacho"),
    ("relator", "relatoria"),
    ("urgencia", "urgencia"),
    ("urgência", "urgencia"),
    ("ordem do dia", "Ordem do Dia"),
    ("pauta", "pauta"),
    ("emenda", "emendas"),
    ("parecer", "parecer"),
    ("terminativ", "terminativa"),
    ("votac", "votacao"),
    ("votaç", "votacao"),
    ("aprovad", "aprovacao"),
    ("rejeitad", "rejeicao"),
    ("redacao final", "redacao final"),
    ("redação final", "redacao final"),
    ("sancao", "sancao"),
    ("sanção", "sancao"),
    ("veto", "veto"),
    ("remessa", "remessa"),
    ("comissao de assuntos economicos", "CAE"),
    ("comissão de assuntos econômicos", "CAE"),
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


def http_get_retry(url, accept, tentativas=TENTATIVAS_HTTP):
    erro = None
    for i in range(tentativas):
        try:
            return http_get(url, accept)
        except Exception as exc:                       # noqa: BLE001
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


# -------------------------------------------------------------- xml utils

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


# ---------------------------------------------------- senado: processo

def coletar_senado():
    """Fatos do processo no Senado. Cada fato e uma string estavel."""
    bruto = ler_fixture("FIXTURE_SENADO")
    if bruto is None:
        bruto = http_get_retry(SENADO_PROC_URL, "application/xml")
    raiz = ET.fromstring(bruto)

    fatos = []
    for caminho, texto in _folhas(raiz):
        if any(r in caminho for r in RUIDO_SENADO):
            continue
        fatos.append("%s :: %s" % (caminho.split("/")[-1], texto))
    return list(dict.fromkeys(fatos))


# Campos do Senado em ordem de utilidade para um alerta legivel.
# Casados em minusculas: o endpoint novo usa tags em camelCase minusculo.
PRIORIDADE_SENADO = (
    ("descricao", "ementa", "despacho", "texto"),      # frases inteiras
    ("situacao", "colegiado", "local", "nome", "data"),  # contexto seco
    ("tramitando", "numero", "sigla"),                 # ultimo recurso
)


def humanizar_senado(fatos):
    """Escolhe, entre os fatos novos, os que valem virar texto de alerta."""
    for nivel in PRIORIDADE_SENADO:
        saida = []
        for f in fatos:
            campo, _, valor = f.partition(" :: ")
            campo = campo.lower()
            if any(k in campo for k in nivel):
                saida.append(valor)
        saida = [x for x in dict.fromkeys(saida) if x]
        if saida:
            return saida
    return []


# ------------------------------------------------- senado: pauta plenario

def _meses_alvo(hoje=None):
    """Primeiro dia do mes corrente e do proximo, no formato AAAAMMDD."""
    hoje = hoje or datetime.date.today()
    primeiro = hoje.replace(day=1)
    if primeiro.month == 12:
        proximo = primeiro.replace(year=primeiro.year + 1, month=1)
    else:
        proximo = primeiro.replace(month=primeiro.month + 1)
    return [primeiro.strftime("%Y%m%d"), proximo.strftime("%Y%m%d")]


def _pauta_de_xml(bruto):
    raiz = ET.fromstring(bruto)
    fatos = []
    for sessao in raiz.iter("Sessao"):
        data = (sessao.findtext("Data") or "").strip()
        tipo = (sessao.findtext("TipoSessao") or "").strip()
        for mat in sessao.iter("Materia"):
            if (mat.findtext("CodigoMateria") or "").strip() != SENADO_CODIGO:
                continue
            partes = [
                data,
                tipo,
                (mat.findtext("DescricaoIdentificacaoMateria") or "").strip(),
                (mat.findtext("DescricaoTipoPauta") or "").strip(),
                (mat.findtext("Apreciacao") or "").strip(),
            ]
            relator = (mat.findtext("NomeRelator") or "").strip()
            if relator:
                partes.append("rel. " + relator)
            fatos.append("PAUTA PLENARIO :: " + " | ".join(p for p in partes if p))
    return fatos


def coletar_pauta_plenario():
    """Sessoes do Plenario do Senado, deste mes e do proximo, com a materia."""
    bruto = ler_fixture("FIXTURE_AGENDA")
    if bruto is not None:
        return _pauta_de_xml(bruto)
    fatos = []
    for mes in _meses_alvo():
        dados = http_get_retry(SENADO_AGENDA_URL % mes, "application/xml")
        fatos.extend(_pauta_de_xml(dados))
    return list(dict.fromkeys(fatos))


# ------------------------------------------------ senado: pauta comissoes

def _reunioes_de_xml(bruto):
    raiz = ET.fromstring(bruto)
    alvo = "<idProcesso>%s</idProcesso>" % SENADO_PROCESSO
    fatos = []
    for reuniao in raiz.iter("reuniao"):
        if alvo not in ET.tostring(reuniao, encoding="unicode"):
            continue
        colegiado = reuniao.find("colegiadoCriador")
        sigla = (colegiado.findtext("sigla") if colegiado is not None else "") or ""
        partes = [
            (reuniao.findtext("dataInicio") or "")[:16].replace("T", " "),
            sigla.strip(),
            (reuniao.findtext("titulo") or "").strip(),
            (reuniao.findtext("situacao") or "").strip(),
        ]
        fatos.append("PAUTA COMISSAO :: " + " | ".join(p for p in partes if p))
    return fatos


def coletar_pauta_comissoes(dias=21):
    """Reunioes de comissao com a materia na pauta, nos proximos <dias> dias."""
    bruto = ler_fixture("FIXTURE_AGENDA_COMISSOES")
    if bruto is None:
        hoje = datetime.date.today()
        fim = hoje + datetime.timedelta(days=dias)
        url = SENADO_REUNIOES_URL % (hoje.strftime("%Y%m%d"), fim.strftime("%Y%m%d"))
        bruto = http_get_retry(url, "application/xml")
    return list(dict.fromkeys(_reunioes_de_xml(bruto)))


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
    except Exception as exc:                           # noqa: BLE001
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
        return DRY_RUN                                 # em dry-run consideramos sucesso
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode({
        "phone": fone,
        "text": texto,
        "apikey": chave,
    })
    try:
        resp = http_get(url, "text/plain")
        # Repo publico: os logs do Actions sao publicos. Mascara digitos
        # para o corpo da resposta nunca vazar o numero de telefone.
        corpo = " ".join(resp.decode("utf-8", "replace").split())
        corpo = "".join("x" if ch.isdigit() else ch for ch in corpo)
        log("  callmebot HTTP 200 (%d bytes): %s" % (len(resp), corpo[:400]))
        return True
    except urllib.error.HTTPError as exc:
        log("  ERRO callmebot: HTTP %s %s" % (exc.code, exc.reason))
    except Exception as exc:                           # noqa: BLE001
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
            time.sleep(5)                              # CallMeBot nao gosta de rajada
    return ok


def classificar(fatos):
    """Rotulos dos eventos-chave presentes nos fatos novos."""
    achados = []
    for f in fatos:
        alvo = f.lower()
        for chave, rotulo in EVENTOS_CHAVE:
            if chave in alvo and rotulo not in achados:
                achados.append(rotulo)
    return achados


# ------------------------------------------------------------------- main

def coletar(nome, funcao, erros):
    """Roda um coletor isolando a falha. Devolve None se a fonte caiu."""
    try:
        return funcao()
    except Exception as exc:                           # noqa: BLE001
        erros.append("%s: %s" % (nome, exc))
        return None


def main():
    log("== monitor PL %s/%s | baseline: %s" % (CAMARA_NUMERO, CAMARA_ANO, STATE_FILE))

    erros = []

    fatos_senado = coletar("Senado", coletar_senado, erros)
    if fatos_senado is not None:
        log("  senado/processo: %d campos lidos" % len(fatos_senado))

    fatos_pauta = coletar("Pauta", coletar_pauta_plenario, erros)
    if fatos_pauta is not None:
        log("  pauta do plenario: %d ocorrencias da materia" % len(fatos_pauta))

    if AGENDA_COMISSOES:
        fatos_comissoes = coletar("Comissoes", coletar_pauta_comissoes, erros)
        if fatos_comissoes is not None:
            log("  pauta de comissoes: %d ocorrencias da materia" % len(fatos_comissoes))
    else:
        fatos_comissoes = None

    resultado_camara = coletar("Camara", coletar_camara, erros)
    if resultado_camara is None:
        fatos_camara, prop_id = None, None
    else:
        fatos_camara, prop_id = resultado_camara
        log("  camara: %d tramitacoes lidas" % len(fatos_camara))

    if fatos_senado is None and fatos_pauta is None and fatos_camara is None:
        log("ERRO: nenhuma fonte respondeu.")
        for e in erros:
            log("  " + e)
        enviar_whatsapp(
            "*PL %s/%s* - MONITOR CEGO\n\n"
            "Nao consegui ler nenhuma das fontes nesta rodada.\n"
            "Sem leitura, nao da para saber se houve movimentacao.\n\n%s"
            % (CAMARA_NUMERO, CAMARA_ANO, SENADO_LINK))
        return 1

    anterior = carregar_estado()
    base = anterior or {}
    primeira_rodada = anterior is None
    if not primeira_rodada and base.get("schema") != SCHEMA:
        # O baseline antigo veio do endpoint depreciado do Senado: os campos
        # tem outros nomes e a comparacao acusaria tudo como novidade.
        log("baseline em formato antigo (schema %s). Regravando sem alertar."
            % base.get("schema"))
        primeira_rodada = True
        base = {}

    def manter(novos, chave):
        return novos if novos is not None else base.get(chave, [])

    novo_estado = {
        "schema": SCHEMA,
        "senado": manter(fatos_senado, "senado"),
        "pauta": manter(fatos_pauta, "pauta"),
        "comissoes": manter(fatos_comissoes, "comissoes"),
        "camara": manter(fatos_camara, "camara"),
        "camara_id": prop_id or base.get("camara_id"),
    }

    if primeira_rodada:
        salvar_estado(novo_estado)
        log("PRIMEIRA RODADA: baseline gravado, nada enviado (comportamento esperado).")
        if erros:
            log("  (com avisos: %s)" % "; ".join(erros))
        return 0

    def diff(chave):
        antes = set(base.get(chave, []))
        return [f for f in novo_estado[chave] if f not in antes]

    novos_senado = diff("senado")
    novos_pauta = diff("pauta")
    novos_comissoes = diff("comissoes")
    novos_camara = diff("camara")

    todos_novos = novos_senado + novos_pauta + novos_comissoes + novos_camara
    houve_novidade = bool(todos_novos)

    if not houve_novidade and not FORCE_NOTIFY:
        log("Sem novidade. Nada enviado.")
        salvar_estado(novo_estado)                     # absorve mudancas de ruido
        return 0

    chaves = classificar(todos_novos)
    if houve_novidade:
        titulo = "*ATENCAO*: " + ", ".join(chaves) if chaves else "novidade na tramitacao"
    else:
        titulo = "heartbeat, sem novidade"
    linhas = ["*PL %s/%s* - %s" % (CAMARA_NUMERO, CAMARA_ANO, titulo)]

    def bloco(rotulo, itens):
        if not itens:
            return
        linhas.append("")
        linhas.append("[%s]" % rotulo)
        for f in itens[:MAX_ITENS_MSG]:
            linhas.append("- " + f)
        if len(itens) > MAX_ITENS_MSG:
            linhas.append("- (+%d outros)" % (len(itens) - MAX_ITENS_MSG))

    bloco("Pauta do Plenario", [f.split(" :: ", 1)[-1] for f in novos_pauta])
    bloco("Pauta de comissao", [f.split(" :: ", 1)[-1] for f in novos_comissoes])

    if novos_senado:
        destaques = humanizar_senado(novos_senado)
        if destaques:
            bloco("Senado", destaques)
        else:
            linhas.append("")
            linhas.append("[Senado]")
            linhas.append("- %d campos mudaram (sem texto descritivo)" % len(novos_senado))

    bloco("Camara", novos_camara)

    if not houve_novidade:
        linhas.append("")
        linhas.append("Canal funcionando. Nenhuma movimentacao nova desde a ultima checagem.")

    linhas.append("")
    linhas.append(SENADO_LINK)
    if novo_estado.get("camara_id"):
        linhas.append("https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao=%s"
                      % novo_estado["camara_id"])
    if erros:
        linhas.append("(fonte indisponivel nesta rodada: %s)"
                      % "; ".join(e.split(":")[0] for e in erros))

    mensagem = "\n".join(linhas)
    log("Novidade: %d senado / %d pauta / %d comissoes / %d camara. Chaves: %s"
        % (len(novos_senado), len(novos_pauta), len(novos_comissoes),
           len(novos_camara), ", ".join(chaves) or "-"))

    if notificar(mensagem):
        salvar_estado(novo_estado)
        log("Enviado e baseline atualizado.")
        return 0

    log("FALHA no envio. Baseline NAO atualizado - tenta de novo na proxima rodada.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
