# B3 Day Trader (`b3_trader`)

Ferramentas de Day Trade em Python padrão (sem dependências obrigatórias) adaptadas para a **B3**, com suporte completo a **WINFUT (Mini Índice)**, **WDOFUT (Mini Dólar)**, **DOLFUT (Dólar Cheio)**, **INDFUT** e **Ações da B3**, em **horário de Brasília (`America/Sao_Paulo`, BRT)** e valores em **Reais (`BRL`)**.

## O que foi adaptado para a B3

- **Contratos e ticks oficiais da B3:**
  - `WINFUT` / `WIN*`: tick de `5` pts, `R$ 0,20/pt` (`R$ 1,00/tick`), lote mínimo `1` contrato, pregão `09:00–18:25 BRT`
  - `WDOFUT` / `WDO*`: tick de `0,5` pt, `R$ 10,00/pt` (`R$ 5,00/tick`), lote mínimo `1` contrato, pregão `09:00–18:30 BRT`
  - `DOLFUT` / `DOL*`: tick de `0,5` pt, `R$ 50,00/pt` (`R$ 25,00/tick`), lote mínimo `5` contratos, pregão `09:00–18:30 BRT`
  - `INDFUT` / `IND*`: tick de `5` pts, `R$ 1,00/pt` (`R$ 5,00/tick`), lote mínimo `5` contratos, pregão `09:00–18:25 BRT`
  - **Ações / ETFs B3** (`PETR4`, `VALE3`, `PETR4F`, `BOVA11`): tick de `R$ 0,01`, lote `100` (ou `1` no fracionário/ETF), pregão `10:00–17:55 BRT`
- **Compra (Long) e Venda (Short):** suporte nativo nos futuros (e opcional em ações via `trader/config.json`).
- **Calendário oficial da B3 (2026–2027):** bloqueia feriados nacionais/B3 e trata abertura às `13:00 BRT` na Quarta-feira de Cinzas.
- **Múltiplas fontes de dados:**
  - `--csv`: lê CSVs exportados do **ProfitChart (Nelogica)**, **MetaTrader 5** ou **Tryd** (detecta `;` ou `,`, formato decimal brasileiro `128.450,00` e datas `DD/MM/YYYY HH:MM`).
  - `--provider mt5`: conecta direto ao **MetaTrader 5** no Windows (`pip install MetaTrader5`).
  - `--provider yahoo`: busca ações da B3 no Yahoo Finance (`.SA` automático).

## Painel Visual Web (MetaTrader 5 BTG + Simulador)

Abra o painel visual no navegador (gráfico de candles 5m em tempo real, sinal ORB 15m, Time Machine com 10.000 replays, boleta 2:1 com validação `PASS`/`BLOCK` e envio de ordens com confirmação):

```bash
python3 -m b3_trader web
# Acesse http://127.0.0.1:8765 no navegador
```

- **Modo Simulador (padrão):** funciona sem precisar instalar nada extra — ótimo para treinar, testar o Time Machine e validar suas travas de risco.
- **Modo MetaTrader 5 (BTG / Windows):** instale `pip install MetaTrader5`, abra o MT5 na sua **conta Demo** com `WIN$N` / `WDO$N` na *Observação do Mercado* e selecione **Modo: MetaTrader 5 (BTG)** no topo do painel. Enquanto `"paper": true` estiver em `trader/config.json`, o sistema bloqueia automaticamente contas reais por segurança.

## Uso via linha de comando (CLI)

```bash
# 1. Cria trader/config.json com limites padrão em BRL
python3 -m b3_trader init

# 2. Verifica horário do pregão B3 e especificações do ativo
python3 -m b3_trader status WINFUT
python3 -m b3_trader status DOLFUT

# 3. Avalia rompimento de abertura (ORB 15 min) ou nível
python3 -m b3_trader entry WINFUT --setup orb --side both --csv seu_arquivo.csv --write

# 4. Calcula stop e alvo arredondados no tick da B3
python3 -m b3_trader exit --stop or-mid --target 2R --write
# Ou manualmente:
python3 -m b3_trader exit --symbol WINFUT --side buy --entry 128500 --stop points:150 --target 2R --write

# 5. Time Machine: 10.000 simulações Monte Carlo + histórico real
python3 -m b3_trader timemachine --csv seu_arquivo.csv --write

# 6. Dimensionamento de contratos/lotes pelo risco em R$ e margem
python3 -m b3_trader budget --write

# 7. Validação final de risco (PASS / BLOCK)
python3 -m b3_trader risk
```

## Testes

```bash
python3 -m unittest discover -s tests -v
```

> **Aviso:** Ferramenta educacional e de apoio à gestão de risco. Não constitui recomendação de investimento e não envia ordens automaticamente para corretoras.
