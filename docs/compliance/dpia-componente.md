# DPIA — Componente Assistente de Voz

**Documento:** DPIA-001  
**Versão:** 1.0-rascunho  
**Data:** 2026-05-09  
**Classificação:** Confidencial  
**Controlador:** \[Nome do app de telemedicina\]  
**Operador:** \[Nome da empresa / componente assistente_de_voz\]

---

## 1. Identificação do componente

O componente `assistente_de_voz` é um serviço de backend que:

1. Captura e transcreve áudio de voz de sessões médicas.
2. Extrai dados estruturados (draft de prontuário) por meio de LLM.
3. Detecta red flags clínicos e orienta o profissional de saúde.
4. Registra um audit log com hash chain para rastreabilidade.

---

## 2. Finalidades do tratamento

| # | Finalidade | Base legal |
|---|---|---|
| 1 | Transcrição de anamnese em consulta | Art. 11 §2 II "a" LGPD (tutela da saúde) |
| 2 | Extração de draft de prontuário | Art. 11 §2 II "a" LGPD |
| 3 | Detecção de red flags clínicos | Art. 11 §2 II "a" LGPD |
| 4 | Audit log de rastreabilidade (5 anos) | Art. 37 LGPD + legítimo interesse do controlador |

---

## 3. Dados tratados

| Categoria | Exemplos | Sensível (Art. 11)? |
|---|---|---|
| Áudio de voz | Gravação da fala do paciente/profissional | Sim |
| Transcrição | Texto da anamnese | Sim |
| Draft de prontuário | Campos estruturados (queixa, medicações, etc.) | Sim |
| Metadados de sessão | session_id, tenant_id, timestamps | Não |
| Audit log | event_type, payload (metadados), hashes | Não (metadados) |

> O componente **não persiste áudio**. O áudio é efêmero — transcrito em streaming e
> descartado em memória ao final da sessão.

---

## 4. Fluxo de dados e sub-operadores

```
Paciente/Profissional → (áudio) → assistente_de_voz
  → Deepgram (STT, EUA)
  → OpenAI / Anthropic (LLM, EUA)
  → Postgres (AuditLog + drafts, [local/cloud a definir])
```

Transferências internacionais para Deepgram, OpenAI e Anthropic requerem DPAs com
cláusula de adequação (Art. 33 LGPD). **Nenhum dado de paciente real antes da assinatura.**

---

## 5. Riscos identificados

| # | Risco | Probabilidade | Impacto | Mitigação |
|---|---|---|---|---|
| R1 | Interceptação de áudio em trânsito | Baixa | Alto | TLS 1.2+ obrigatório em todos os endpoints |
| R2 | Acesso não autorizado ao banco | Baixa | Alto | RLS por tenant_id; acesso DBA ≤ 3 pessoas |
| R3 | Reescrita do AuditLog por DBA com acesso root | Baixa | Alto | Ver Seção 6 (risco residual) |
| R4 | Vazamento de transcrição via LLM externo | Baixa | Alto | DPA com sub-operadores; zero-retention policy |
| R5 | Inferência incorreta de red flag | Média | Alto | Revisão humana (HITL) obrigatória antes de decisão clínica |

---

## 6. Risco residual — imutabilidade do AuditLog

O hash chain detecta adulteração, mas um administrador de banco (DBA) com acesso privilegiado
pode reescrever eventos em cascade (UPDATE/DELETE direto no Postgres), destruindo a
integridade da cadeia sem rastro na própria tabela.

**Mitigações aceitas na versão 2.0:**

(a) Acesso DBA restrito a ≤ 3 pessoas nomeadas com processo formal de aprovação.  
(b) CloudTrail / PostgreSQL audit log registra toda operação privilegiada (ALTER, DELETE,
    UPDATE direto em `audit_log`).  
(c) Export diário de checkpoints do hash chain para S3 imutável programado para a versão
    2.1 — até lá o risco residual é aceito formalmente abaixo.
(d) Desde a migration `0006` (2026-10-27): trigger `trg_audit_log_append_only` bloqueia
    `UPDATE`/`DELETE` em `audit_log`; `verify_chain` detecta linha alterada (`hash`) e linha
    removida/inserida (`link`); hash cobre `event_type`, `session_id`, `actor_sub` e `payload`
    (ADR 0004). Reescrever exige desabilitar a trigger — ação privilegiada registrada em (b).

> **Este risco residual deve ser formalmente aceito e assinado antes do primeiro paciente real.**

### Aceite formal de risco residual

| Campo | Valor |
|---|---|
| Risco | Reescrita do AuditLog por DBA privilegiado |
| Versão | 2.0 |
| Mitigações em produção | (a) e (b) acima |
| Data limite para mitigação (c) | Versão 2.1 |
| **[ ] Aceite formal assinado:** | _________________________ |
| Cargo / Nome | |
| Data | |

---

## 7. Retenção e eliminação

| Dado | Retenção | Método de eliminação |
|---|---|---|
| Áudio | Efêmero | Descarte em memória |
| Transcrição / draft | 6–12 meses | DELETE controlado pelo Controlador |
| AuditLog | 5 anos | Expurgo automatizado após 5 anos |

---

## 8. Direitos dos titulares

O Controlador é responsável por atender solicitações de acesso, correção, portabilidade e
eliminação (Arts. 18-20 LGPD). O Operador fornecerá extrato de dados mediante instrução
formal do Controlador em até 5 dias úteis.

---

## 9. DPO

DPO compartilhado com o Controlador. Nomeação formal registrada conforme Art. 41 LGPD.

**[ ] DPO indicado e nomeação formal registrada**

---

## 10. Aprovação

| Papel | Nome | Data | Assinatura |
|---|---|---|---|
| Responsável técnico | | | |
| DPO | | | |
| Representante do Controlador | | | |
