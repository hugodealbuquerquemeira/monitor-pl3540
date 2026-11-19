#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor SUSEP/SES - BB Seguridade (BBSE3): lucro e resultado financeiro mensais
das investidas que reportam a SUSEP, ponderados pela participacao da BBSE.

  Brasilseg   FIP 06785  Seguradoras: Demonstracoes Contabeis      74,99%
  Brasilprev  FIP 04707  Seguradoras: Demonstracoes Contabeis      74,99%
  Brasilcap   FIP 20141  Capitalizacao: Demonstracoes Contabeis    66,77%
  (BB Corretora nao reporta a SUSEP - fica de fora)

A DRE do SES e ACUMULADA NO ANO: mes = acum(m) - acum(m-1); janeiro = acum(jan).

Quando o MES_ALVO aparece no SES manda WhatsApp (CallMeBot) com:
  - LL atribuivel do mes (soma ponderada) e por empresa
  - resultado financeiro atribuivel do mes x meses anteriores (jan..alvo)
  - trimestre do alvo: soma e run-rate (x consenso, se CONSENSO_TRI_MI)
  - grafico PNG (LL e resultado financeiro mensais, empilhados por empresa)
Se as empresas sairem em dias diferentes (seguradoras x capitalizacao), manda o
parcial e depois o completo.

Reaproveita o acesso ao SES e o envio do monitor_susep_irb.py.

Variaveis de ambiente (alem das do monitor do IRB):
  MES_ALVO             AAAAMM
  STATE_FILE           default state/susep_bbse_state.json
  PARTICIPACOES        ex. "06785:74.99,04707:74.99,20141:66.77"
  CONSENSO_TRI_MI      consenso de LL atribuivel do trimestre (R$ mi), opcional
  FIXTURE_DIR          teste offline: arquivos dre_<FIP>_<AAAAMM>.html
"""

import os
import sys
import time

import monitor_susep_irb as base

log, mi, pega, notificar = base.log, base.mi, base.pega, base.notificar
rotulo_mes, rotulo_curto, mes_anterior = base.rotulo_mes, base.rotulo_curto, base.mes_anterior

MES_ALVO = os.environ.get("MES_ALVO", "202610").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state/susep_bbse_state.json")
CONSENSO_TRI_MI = os.environ.get("CONSENSO_TRI_MI", "").strip().replace(",", ".")

SEG_FORM = base.SES_BASE + "balanco.aspx?tipo=seg&id=14"
CAP_FORM = base.SES_BASE + "balanco.aspx?tipo=cap&id=36"

EMPRESAS = [  # (nome, FIP, formulario, participacao default %)
    ("Brasilseg", "06785", SEG_FORM, 74.99),
    ("Brasilprev", "04707", SEG_FORM, 74.99),
    ("Brasilcap", "20141", CAP_FORM, 66.77),
]
_part_env = {}
for _p in os.environ.get("PARTICIPACOES", "").replace(" ", "").split(","):
    if ":" in _p:
        _k, _v = _p.split(":", 1)
        _part_env[_k] = float(_v.replace(",", "."))
PART = {fip: _part_env.get(fip, pct) / 100.0 for _, fip, _, pct in EMPRESAS}

LL = base.LL
RF = "(+) RESULTADO FINANCEIRO"
LAIR = "(=) RESULTADO ANTES DOS IMPOSTOS"
CORES = {"06785": "#1f4e79", "04707": "#2e86c1", "20141": "#a9cce3"}

# ------------------------------------------------------------------ dados

_cache = {}


def dre(fip, form, mes):
    chave = (fip, mes)
    if chave in _cache:
        return _cache[chave]
    pasta = os.environ.get("FIXTURE_DIR")
    if pasta:
        caminho = os.path.join(pasta, "dre_%s_%s.html" % (fip, mes))
        txt = open(caminho, encoding="utf-8").read() if os.path.exists(caminho) else ""
        res = base.parse_dre(txt)
    else:
        res = base.buscar_dre_ses(form, fip, mes)
    _cache[chave] = res
    return res


def mensal(fip, form, mes, conta):
    """Valor do mes (acum. mes - acum. mes anterior). None se faltar dado."""
    a = pega(dre(fip, form, mes), conta)
    if a is None:
        return None
    if mes.endswith("01"):
        return a
    b = pega(dre(fip, form, mes_anterior(mes)), conta)
    return None if b is None else a - b


def meses_do_ano():
    ano = MES_ALVO[:4]
    return ["%s%02d" % (ano, m) for m in range(1, int(MES_ALVO[4:]) + 1)]


def serie(conta, fips):
    """{fip: [(mes, valor 100%)]} de jan..alvo."""
    out = {}
    for nome, fip, form, _ in EMPRESAS:
        if fip in fips:
            out[fip] = [(m, mensal(fip, form, m, conta)) for m in meses_do_ano()]
    return out


def atribuivel(ser):
    """[(mes, soma ponderada)] - None se alguma empresa estiver sem o mes."""
    meses = meses_do_ano()
    res = []
    for i, m in enumerate(meses):
        vals = [ser[f][i][1] for f in ser]
        res.append((m, None if any(v is None for v in vals)
                    else sum(ser[f][i][1] * PART[f] for f in ser)))
    return res

# ---------------------------------------------------------------- formato


def pct(v):
    return ("%+.1f%%" % (v * 100)).replace(".", ",")


def grafico_texto(pontos, titulo):
    vals = [abs(v) for _, v in pontos if v is not None]
    escala = max(vals) if vals else 1.0
    linhas = [titulo, "```"]
    for mes, v in pontos:
        if v is None:
            linhas.append("%s    n/d" % rotulo_curto(mes))
            continue
        n = max(1, int(round(12 * abs(v) / escala))) if v else 0
        linhas.append("%s %6.1f %s" % (rotulo_curto(mes), v / 1e6, ("█" if v >= 0 else "▒") * n))
    linhas.append("```")
    return "\n".join(linhas)


def bloco_trimestre(ll_attr):
    ano, m = MES_ALVO[:4], int(MES_ALVO[4:])
    tri = (m - 1) // 3 + 1
    meses = ["%s%02d" % (ano, x) for x in range(3 * tri - 2, m + 1)]
    vals = dict(ll_attr)
    if any(vals.get(x) is None for x in meses):
        return []
    soma = sum(vals[x] for x in meses)
    n = len(meses)
    impl = soma / n * 3
    nome = "%dQ%s" % (tri, ano[2:])
    rot = " + ".join(rotulo_curto(x) for x in meses)
    linhas = ["", "*%s (LL atribuivel)*" % nome, "- Somado %s: %s" % (rot, mi(soma))]
    linhas.append(("- Run-rate %s (x3/%d): *%s*" % (nome, n, mi(impl))) if n < 3
                  else "- %s fechado: *%s*" % (nome, mi(impl)))
    if CONSENSO_TRI_MI:
        cons = float(CONSENSO_TRI_MI) * 1e6
        d = impl / cons - 1
        linhas += ["- Consenso %s: %s" % (nome, mi(cons)),
                   "- *%s: %s*" % ("MISS" if d < 0 else "BEAT", pct(d))]
    return linhas


def montar(saiu, titulo, ll_ser, rf_ser):
    ll_attr, rf_attr = atribuivel(ll_ser), atribuivel(rf_ser)
    ll_m, rf_m = dict(ll_attr), dict(rf_attr)
    alvo, ant = MES_ALVO, mes_anterior(MES_ALVO)
    nomes = {fip: nome for nome, fip, _, _ in EMPRESAS}
    falta = [nomes[f] for _, f, _, _ in EMPRESAS if f not in saiu]

    L = ["*BBSE (BBSE3) - SUSEP %s* - %s" % (rotulo_mes(alvo), titulo)]
    if falta:
        L.append("_Parcial: falta %s no SES. Soma abaixo so com as que sairam._"
                 % ", ".join(falta))
    L += ["", "*LL atribuivel %s: %s*" % (rotulo_curto(alvo), mi(ll_m.get(alvo)))]
    if not alvo.endswith("01") and ll_m.get(ant) is not None:
        L.append("Mes anterior (%s): %s" % (rotulo_curto(ant), mi(ll_m[ant])))
    L += ["", "[100% | atribuivel]"]
    for nome, fip, form, _ in EMPRESAS:
        if fip not in saiu:
            continue
        ll = dict(ll_ser[fip]).get(alvo)
        rf = dict(rf_ser[fip]).get(alvo)
        L.append("- %s (%s%%): LL %s | %s; RF %s" % (
            nome, ("%.2f" % (PART[fip] * 100)).replace(".", ","),
            mi(ll), mi(None if ll is None else ll * PART[fip]), mi(rf)))

    # resultado financeiro: mes x historico do ano
    rf_alvo = rf_m.get(alvo)
    hist = [v for m, v in rf_attr if m != alvo and v is not None]
    L += ["", "*Resultado financeiro atribuivel %s: %s*" % (rotulo_curto(alvo), mi(rf_alvo))]
    if rf_alvo is not None and hist:
        media = sum(hist) / len(hist)
        if rf_m.get(ant) is not None:
            L.append("- vs %s (%s): %s" % (rotulo_curto(ant), mi(rf_m[ant]),
                                           pct(rf_alvo / rf_m[ant] - 1) if rf_m[ant] else "n/d"))
        L.append("- vs media jan-%s (%s): %s" % (rotulo_curto(ant)[:3], mi(media),
                                                 pct(rf_alvo / media - 1) if media else "n/d"))
        L.append("- Acum. %s: %s" % (MES_ALVO[:4],
                                      mi(sum(v for _, v in rf_attr if v is not None))))
    L += bloco_trimestre(ll_attr)
    return "\n".join(L), ll_attr, rf_attr

# ---------------------------------------------------------------- grafico


def grafico_png(ll_ser, rf_ser, caminho):
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
        return ("%.0f" % v).replace(".", ",")

    meses = meses_do_ano()
    xs = list(range(len(meses)))
    nomes = {fip: nome for nome, fip, _, _ in EMPRESAS}
    fig, eixos = plt.subplots(2, 1, figsize=(10, 8), dpi=150, sharex=True)
    for ax, ser, tit in ((eixos[0], ll_ser, "Lucro liquido atribuivel"),
                         (eixos[1], rf_ser, "Resultado financeiro atribuivel")):
        pos, neg = [0.0] * len(meses), [0.0] * len(meses)
        tot = [0.0] * len(meses)
        for fip in ser:
            vals = [(v or 0) * PART[fip] / 1e6 for _, v in ser[fip]]
            bottoms = [pos[i] if v >= 0 else neg[i] for i, v in enumerate(vals)]
            ax.bar(xs, vals, bottom=bottoms, color=CORES[fip], width=0.75,
                   label="%s (%s%%)" % (nomes[fip], ("%.2f" % (PART[fip] * 100)).replace(".", ",")))
            for i, v in enumerate(vals):
                if v >= 0:
                    pos[i] += v
                else:
                    neg[i] += v
                tot[i] += v
        for i, t in enumerate(tot):
            ax.annotate(br(t), (i, pos[i]), xytext=(0, 3), textcoords="offset points",
                        ha="center", fontsize=8, fontweight="bold" if i == len(tot) - 1 else None)
        hist = tot[:-1]
        if hist:
            media = sum(hist) / len(hist)
            ax.axhline(media, color="#e67e22", lw=1, ls="--")
            ax.text(len(meses) - 0.5, media, " media jan-%s: %s" % (
                rotulo_curto(meses[-2])[:3] if len(meses) > 1 else "", br(media)),
                color="#e67e22", fontsize=8, va="bottom", ha="right")
        ax.axhline(0, color="#555", lw=0.8)
        ax.set_title(tit + " (R$ mi)", fontsize=11, loc="left")
        ax.set_ylim(top=max(pos) * 1.15 if max(pos) > 0 else None)
        ax.spines[["top", "right"]].set_visible(False)
    eixos[0].legend(fontsize=8, frameon=False, ncol=3, loc="upper left")
    eixos[1].set_xticks(xs)
    eixos[1].set_xticklabels([rotulo_curto(m) for m in meses], fontsize=9)
    fig.suptitle("BB Seguridade - investidas na SUSEP/SES, mensal %s" % MES_ALVO[:4],
                 fontsize=12)
    fig.text(0.99, 0.01, "Mes = DRE acumulada do mes - acumulada do mes anterior. "
             "Ponderado pela participacao da BBSE. Sem BB Corretora.",
             ha="right", fontsize=7, color="#777")
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
    fig.savefig(caminho)
    plt.close(fig)

# ------------------------------------------------------------------- main


def main():
    base.MES_ALVO = MES_ALVO   # o modulo base usa isso em rotulos
    evento = os.environ.get("GITHUB_EVENT_NAME", "")
    hoje = base.agora_brt().date().isoformat()
    inicio, fim = os.environ.get("INICIO", "").strip(), os.environ.get("FIM", "").strip()
    if evento == "schedule":
        if inicio and hoje < inicio:
            log("Antes de INICIO=%s. Nada a fazer." % inicio)
            return 0
        if fim and hoje > fim:
            log("Depois de FIM=%s. Nada a fazer." % fim)
            return 0
        if base._flag("DIAS_UTEIS") and base.agora_brt().weekday() >= 5:
            log("Fim de semana. Nada a fazer.")
            return 0

    log("== monitor SUSEP BBSE | alvo %s | estado %s" % (MES_ALVO, STATE_FILE))
    base.STATE_FILE = STATE_FILE
    estado = base.carregar_estado()
    if estado.get("mes_alvo") != MES_ALVO:
        estado = {"mes_alvo": MES_ALVO}

    saiu, erros = [], []
    for nome, fip, form, _ in EMPRESAS:
        try:
            ok = pega(dre(fip, form, MES_ALVO), LL) is not None
        except Exception as exc:  # noqa: BLE001
            erros.append("%s: %s" % (nome, str(exc)[:80]))
            continue
        log("  %s %s: %s" % (nome, MES_ALVO, "PUBLICADO" if ok else "ainda nao"))
        if ok:
            saiu.append(fip)

    if not saiu:
        if base.heartbeat_agora():
            txt = ("*BBSE SUSEP %s* - heartbeat %s\n" % (
                rotulo_mes(MES_ALVO), base.agora_brt().strftime("%d/%m %H:%M")))
            txt += ("SES NAO respondeu: %s" % "; ".join(erros)) if erros else \
                "Ainda nao publicado (Brasilseg, Brasilprev, Brasilcap). Monitor OK."
            return 0 if base.enviar_whatsapp(txt) else 1
        return 1 if erros and len(erros) == len(EMPRESAS) else 0

    try:
        assinatura = {f: [pega(dre(f, fm, MES_ALVO), LL),
                          pega(dre(f, fm, mes_anterior(MES_ALVO)), LL)]
                      for _, f, fm, _ in EMPRESAS if f in saiu}
    except Exception as exc:  # noqa: BLE001
        log("ERRO lendo mes base: %s" % exc)
        return 1
    anterior = estado.get("assinatura") or {}
    if anterior == assinatura and not base.FORCE_NOTIFY:
        log("Ja notificado em %s, sem novidade." % estado.get("quando"))
        return 0

    completo = len(saiu) == len(EMPRESAS)
    revisado = any(f in anterior and anterior[f] != assinatura[f] for f in assinatura)
    if revisado:
        titulo = "*DADO REVISADO* pela SUSEP"
    elif anterior and anterior == assinatura:
        titulo = "reenvio (teste)"
    elif anterior:
        titulo = "*COMPLETO*" if completo else "*SAIU MAIS UMA*"
    else:
        titulo = "*SAIU*"

    try:
        ll_ser, rf_ser = serie(LL, saiu), serie(RF, saiu)
    except Exception as exc:  # noqa: BLE001
        log("ERRO montando series: %s" % exc)
        return 1
    msg, ll_attr, rf_attr = montar(saiu, titulo, ll_ser, rf_ser)

    extra = [grafico_texto(ll_attr, "*LL atribuivel mensal (R$ mi)*"), "",
             grafico_texto(rf_attr, "*Resultado financeiro atribuivel mensal (R$ mi)*")]
    try:
        png = "charts/bbse_susep_%s.png" % MES_ALVO
        grafico_png(ll_ser, rf_ser, png)
        log("  grafico gravado em %s" % png)
        url = base.publicar_png(png, "grafico: BBSE SUSEP %s [skip ci]" % MES_ALVO)
        if url:
            extra += ["", "Grafico: " + url]
    except Exception as exc:  # noqa: BLE001
        log("  aviso: grafico PNG falhou (%s)" % exc)
    extra += ["", "Fonte: SES/SUSEP, DRE - FIP 06785, 04707, 20141"]
    msg2 = "*BBSE* - " + "\n".join(extra)
    log(msg)
    log(msg2)

    if not notificar(msg):
        log("FALHA no envio. Estado NAO gravado - reenvia na proxima rodada.")
        return 1
    time.sleep(5)
    if not notificar(msg2):
        log("  aviso: mensagem dos graficos nao foi enviada")
    estado.update({
        "assinatura": assinatura, "saiu": saiu, "completo": completo,
        "ll_attr": ll_attr, "rf_attr": rf_attr,
        "quando": base.agora_brt().strftime("%Y-%m-%d %H:%M BRT")})
    base.salvar_estado(estado)
    log("Enviado e estado gravado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
