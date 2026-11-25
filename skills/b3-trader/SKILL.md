---
name: b3-trader
description: >-
  Assistente de Day Trade para a B3 (WINFUT / Mini Índice, WDOFUT / Mini Dólar,
  DOLFUT / Dólar Cheio, INDFUT e Ações da B3) em horário de Brasília (BRT) e
  reais (BRL). Suporta compra (long) e venda (short), calendário oficial de
  feriados da B3 (2026–2027), arredondamento por tick (5 pts no WIN, 0,5 pt no
  WDO/DOL, R$ 0,01 em ações), dados via MetaTrader 5, CSV do ProfitChart/MT5/Tryd
  ou Yahoo Finance (.SA). Executa o fluxo completo: status do pregão, sinal de
  entrada (ORB ou nível), plano de saída (stop/alvo), Time Machine (10.000
  replays Monte Carlo + histórico), dimensionamento de contratos/lotes e gestão
  de risco (PASS/BLOCK). Use quando o usuário falar de Mini Índice, WINFUT,
  Mini Dólar, WDOFUT, DOLFUT, ações da B3 (PETR4, VALE3, etc.) ou pregão B3.
---

# b3-trader

Toolkit de day trade adaptado para a **B3** no repositório `/home/antigravity/raffaelcandido/Trade`.
Usa exclusivamente Python padrão (sem dependências obrigatórias), horário de **Brasília (`America/Sao_Paulo`, BRT)** e valores em **Reais (`BRL`)**.

## Especificações suportadas automaticamente

| Ativo | Tick | Valor por ponto | Valor por tick | Lote mínimo | Pregão (BRT) |
| --- | --- | --- | --- | --- | --- |
| `WINFUT` / `WIN*` | 5 pts | R$ 0,20 | R$ 1,00 | 1 contrato | 09:00–18:25 |
| `INDFUT` / `IND*` | 5 pts | R$ 1,00 | R$ 5,00 | 5 contratos | 09:00–18:25 |
| `WDOFUT` / `WDO*` | 0,5 pt | R$ 10,00 | R$ 5,00 | 1 contrato | 09:00–18:30 |
| `DOLFUT` / `DOL*` | 0,5 pt | R$ 50,00 | R$ 25,00 | 5 contratos | 09:00–18:30 |
| Ações (`PETR4`, `VALE3`) | R$ 0,01 | R$ 1,00 | R$ 0,01 | 100 (ou 1 em `F`/ETF) | 10:00–17:55 |

## Fontes de dados (`--provider` / `--csv`)

- **`--csv arquivo.csv`**: lê CSV exportado do **ProfitChart (Nelogica)**, **MetaTrader 5** ou **Tryd** (detecta `;` ou `,`, vírgula decimal brasileira `128.450,00` e datas `DD/MM/YYYY HH:MM` ou ISO).
- **`--provider mt5`**: lê barras direto do terminal **MetaTrader 5** no Windows (`pip install MetaTrader5`).
- **`--provider yahoo`**: lê ações e índices da B3 no Yahoo Finance (adiciona `.SA` automaticamente). Não fornece futuros intraday.

## Fluxo de comandos (executar em `/home/antigravity/raffaelcandido/Trade`)

```bash
# 1. Inicializar configuração padrão B3 (trader/config.json)
python3 -m b3_trader init

# 2. Conferir horário do pregão B3 e especificações do contrato
python3 -m b3_trader status WINFUT
python3 -m b3_trader status DOLFUT

# 3. Sinal de entrada (ORB 15m compra/venda ou nível) -> trader/entry.json
python3 -m b3_trader entry WINFUT --setup orb --side both --csv dados/winfut_5m.csv --write

# 4. Plano de saída (stop e alvo arredondados no tick) -> trader/plan.json
python3 -m b3_trader exit --stop or-mid --target 2R --write
# Ou manual:
python3 -m b3_trader exit --symbol WINFUT --side sell --entry 128500 --stop points:150 --target 2R --write

# 5. Time Machine (10.000 replays Monte Carlo + histórico real) -> trader/timemachine.json
python3 -m b3_trader timemachine --csv dados/winfut_5m.csv --write

# 6. Dimensionamento de contratos/lotes em BRL -> trader/orders.json
python3 -m b3_trader budget --write

# 7. Validação de risco (PASS / BLOCK) -> trader/state/risk.json
python3 -m b3_trader risk
```

## Segurança

- Ferramenta educacional e de gestão de risco; não constitui recomendação de investimento.
- Nunca envia ordens automaticamente para a corretora (`paper: true` por padrão).
- Toda nova entrada exige stop-loss válido, relação R:R mínima e respeito ao limite de perda por trade e diário em BRL.
