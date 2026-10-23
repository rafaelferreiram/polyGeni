# Auditoria P0 — Integridade e proteção do capital (Savoia 2.0)

**Data:** 23/10/2026 · **Branch:** `feature/savoia-p0-integridade` (PR #56) · **Baseline:** `ad107db` (main)

## 1. Baseline reproduzível

- Suíte de testes: **200 passando** em `ad107db` (as 19 falhas iniciais eram ambiente — faltavam `anthropic` e `hyperliquid` no `.venv`; resolvido com `pip install -r requirements.txt`).
- Após as correções P0: **206 passando** (6 regressões novas, 1 teste reescrito para o novo contrato da cota).
- Nenhuma operação real foi executada na auditoria; consultas ao `live_mainnet.db` em modo somente leitura.

## 2. Incidente de 22/10 — causa raiz CONFIRMADA

Linha do tempo (decisions do `live_mainnet.db`, UTC):

- 16:21 entrada NEAR (`trend`), depois UNI/DOGE/SUI/HYPE (whales) entre 19:14 e 22:06.
- 17:12 em diante: erros 429 (rate limit) em mark-to-market.
- 22:09 HYPE fechada por "max holding 2h" — o limite de 2h **ainda não existia em produção**; essa saída veio de `TURBO`/regras próprias.
- 23:05–23:26: posição HYPE na exchange sem registro no ledger ("comprada pelo Evair durante um reinício do app") → kill switch em loop até adoção manual às 23:26.
- **01:33:06** deploy do commit `0b61777` (cap `MAX_HOLD_CAP_H=2h` para todo trade).
- **01:33:31–01:33:53** NEAR (9,2h), UNI, DOGE, SUI, HYPE fechadas em sequência por "max holding time 2h reached" = −$0,74.

**Conclusão:** não foi scheduler travado nem ciclo interrompido. O cap novo foi aplicado **retroativamente** a posições abertas sob a regra antiga (trend 24h / default 72h). O segundo problema real da noite foi a órfã HYPE (crash entre o fill e a gravação no ledger).

Problema latente adicional (não foi a causa, mas é real): em `engine.mark_and_exit`, um erro no mark (`429`) fazia `continue` e **pulava a avaliação de saída por tempo** daquela posição — numa tempestade de 429 prolongada, posições poderiam passar do cap.

## 3. Problemas encontrados (arquivo:linha no baseline) e correções

| # | Problema | Onde | Correção |
|---|---|---|---|
| 1 | Cap de hold aplicado retroativamente em deploy | `src/solana/engine.py` (`_max_hold` em `mark_and_exit`) | Limite vigente na entrada gravado em `state hold:<order_id>`; `_hold_limit()` usa o gravado; config nova só governa posições novas |
| 2 | Mark falho pulava saída por tempo | `src/solana/engine.py:236-241` | Saída por tempo avaliada antes/apesar do mark; fecho "pelo tempo" sem cotação |
| 3 | Órfã após crash entre fill e ledger | `src/solana/hl_live.py` (`buy`) | Diário `pending:<order_id>` antes do envio; `sync()._adopt_pending` registra o fill da exchange, repõe stop/alvo (reusa triggers que sobraram), fecha se proteção falhar; varredura de intenções >1h |
| 4 | IA sem freio diário ($1,77 num dia = 79% do mês) | `src/solana/costs.py` (`check_budget`) | `AI_DAILY_BUDGET_USD` (padrão $0,25; 0 desliga) |
| 5 | Cota diária afrouxava entradas e encurtava alvo na conta real | `src/solana/engine.py:198-224,647` | Observacional por padrão (medida, logada, sem efeito); rollback `QUOTA_RELAX_ENTRIES=1`; reservas mantêm `RESERVE_RELAX` no lab |
| 6 | Promoção manual não exigia lucro | `src/solana/arena.py` (`can_promote`) | Exige lucro líquido positivo após custo de IA, além das 12 vitórias, ≥35% e p<0,05 (regra do dono preservada) |
| 7 | Reinício podia repetir o sinal do mesmo candle | `src/solana/engine.py:643` (`_entered` em memória) | Persistido no ledger (`state entered`, últimos 100) |

## 4. Vias de acesso à conta real (auditadas, sem mudanças de flag)

1. `REAL_TRUSTED_BOTS` (`rules`, `trend`) — validados por backtest, decisão do dono (21/10).
2. `green_light` — 20 trades, 12 vitórias, lucro líquido, WR ≥ 60%, PF ≥ 1,2.
3. Promoção manual (`can_promote`) — agora também exige lucro líquido (item 6 acima).
4. `LIVE_FAST_ALLOW` — **bypass real**: `whales_15m`, `copy_15m`, `trend4h_15m` operam o real sem green light, long-only, $10, 1 posição aberta, lista explícita (`LIVE_FAST_BOTS`). Decisão do dono (22/10). **Mantido**; recomendação: condicionar à expectância recente positiva no lab (precisa de aprovação do dono — ver §6).
5. Shorts reais: bloqueio duro em `hl_live.buy` sem `LIVE_ALLOW_SHORT=1` + bot no `SHORT_BOTS` + `short_gate` (regime, limites). OK.

Proteções já sólidas confirmadas: RiskAgent determinístico com fingerprint (Executor recusa divergência), cloid idempotente por `order_id`, proteção SL/TP na exchange ou fecho imediato, sync com exchange como verdade, kill switch por perda diária/drift/exchange inacessível, livros append-only.

## 5. Comparação antes/depois

- Financeiro: sem mudança de limites; efeito esperado é defensivo (não repetir o −$0,74 do fecho em massa; não queimar >$0,25/dia de IA; não afrouxar entrada por cota). Medir nos próximos 7 dias: custo de IA/dia, nº de adoções automáticas, saídas "pelo tempo" sem cotação.
- Testes: 200 → 206.

## 6. Mudanças que PRECISAM de autorização do dono (não feitas)

1. Condicionar `LIVE_FAST_ALLOW` a expectância recente positiva no lab (hoje é incondicional dentro da lista).
2. Rebaixar `whales` do real enquanto o filtro "só em tendência de alta" não for validado no lab (relatório da manhã de 22/10: 8/14 perdas no real, 32,7% no lab).
3. Qualquer alteração de `LIVE_MAX_*`, alavancagem, `REAL_TRUSTED_BOTS` ou shorts reais.

## 7. Rollback

- Cota: `QUOTA_RELAX_ENTRIES=1`. · IA diária: `AI_DAILY_BUDGET_USD=0`. · Demais: reverter o commit do PR #56.
- As chaves `hold:`/`pending:`/`entered` no state são inofensivas se o código antigo voltar (ignoradas).

## 8. Próximos passos (plano Savoia 2.0)

- **P1** Market Intelligence: scanner dinâmico de oportunidades (volume×volatilidade×spread×custo), classificador de regime (comparar contra o filtro BTC-50h antes de trocar), contrato padronizado de sinal (§20 do master prompt), cache compartilhado de dados/MCPs.
- **P2** Multi-timeframe: dados de alta resolução via WebSocket antes de validar scalping (os backtests 15m existentes são negativos — scalp fica no laboratório).
- **P3** Academia: significance point-in-time (bootstrap em blocos, walk-forward), champion/challenger, fim da troca de reservas por 12h sem trade (avaliar por oportunidades reais, não por atividade).
- **P4** Fast Lane / Intelligence Lane: saída por tempo e proteção num watchdog independente do ciclo de entradas; Claude/MCP fora do caminho crítico.
- **P5** Match Center: expor cota observacional, custo de IA diário, adoções e motivos de bloqueio no painel.
