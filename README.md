# Monitor do PL 3540/2026

Checa a tramitação do **PL 3540/2026** (CSLL/IRPJ de resseguradoras locais — Dep. Isnaldo
Bulhões Jr., aprovado na Câmara e remetido ao Senado em 13/08/2026) e avisa no WhatsApp
quando aparece movimentação nova.

## Como funciona

- Lê os **dados abertos do Senado** (matéria `175500`, XML) e da **Câmara**
  (API v2, JSON) a cada execução.
- Compara com o baseline em `state/pl3540_state.json`.
- Se apareceu algo novo, manda WhatsApp via **CallMeBot** e regrava o baseline.
- Se o envio falhar, o baseline **não** é regravado — a novidade é reenviada na
  rodada seguinte, então nada se perde.
- Só biblioteca padrão do Python: nenhum `pip install`.

Roda de segunda a sexta às 8h, 11h, 14h, 17h e 20h (horário de Brasília), mais o
botão **Run workflow** na aba Actions.

## Secrets necessários

| Secret | Valor |
| --- | --- |
| `WHATSAPP_PHONE` | seu número com DDI, ex. `+5511999999999` |
| `CALLMEBOT_APIKEY` | a apikey que o bot do CallMeBot devolveu |

## Testar

- **Sem novidade, só validar o canal:** aba Actions → Run workflow → marque
  `force_notify`. Ele manda uma mensagem de teste mesmo sem movimentação.
- **Localmente, sem enviar nada:** `DRY_RUN=1 python monitor_pl3540.py`

## Variáveis de ambiente

| Variável | Default | Para quê |
| --- | --- | --- |
| `STATE_FILE` | `state/pl3540_state.json` | onde fica o baseline |
| `SENADO_CODIGO` | `175500` | código da matéria no Senado |
| `CAMARA_NUMERO` / `CAMARA_ANO` | `3540` / `2026` | proposição na Câmara |
| `DRY_RUN` | — | `1` imprime em vez de enviar |
| `FORCE_NOTIFY` | — | `1` envia mesmo sem novidade |

Para monitorar outro projeto, mude os secrets não — mude essas variáveis no
workflow (e apague o `state/` para regravar o baseline).

## Detalhes chatos, mas importantes

- A primeira execução **não manda nada**: ela só grava o baseline. É o esperado.
- O GitHub desativa workflows agendados após ~60 dias sem atividade no
  repositório. Ele avisa por e-mail; basta reativar em Actions.
- Cron do GitHub Actions costuma atrasar alguns minutos em horário de pico. Para
  acompanhar um projeto de lei isso é irrelevante.
- O CallMeBot recebe a apikey na query string da URL. É como a API dele funciona,
  mas significa que a chave passa pela URL. Ela vale só para mandar mensagem para
  o seu próprio número; se quiser algo mais fechado, um bot do Telegram (token no
  corpo do POST) ou e-mail via SMTP resolvem o mesmo problema.

---

# Monitor SUSEP — IRB + BB Seguridade (lucro mensal)

Workflow `susep-irb-monitor.yml`. Dias úteis, 9h02 / 12h02 / 16h02 BRT, entre `INICIO` e `FIM`.
Heartbeat só na rodada das 9h (`HEARTBEAT_HORAS`). Quando o `MES_ALVO` aparece no SES, manda o alerta uma vez.

- **IRB** (`monitor_susep_irb.py`): FIP 31623, Resseguradoras Locais → DRE. Lucro do mês, DRE do mês, trimestre x consenso, gráfico.
- **BBSE** (`monitor_susep_bbse.py`): Brasilseg (06785) e Brasilprev (04707) em Seguradoras → DRE, Brasilcap (20141) em Capitalização → DRE.
  LL e resultado financeiro do mês, a 100% e atribuível (74,99% / 74,99% / 66,77%, em `PARTICIPACOES`),
  resultado financeiro x mês anterior e x média do ano, trimestre (run-rate), gráfico jan→alvo.
  Se seguradoras e capitalização saírem em dias diferentes, manda o parcial e depois o completo. BB Corretora não reporta à SUSEP.

Teste manual: Actions → Run workflow → `qual` = irb / bbse / ambos.
Teste offline do BBSE: `FIXTURE_DIR=<pasta com dre_<FIP>_<AAAAMM>.html> DRY_RUN=1 MES_ALVO=202609 python monitor_susep_bbse.py`.
