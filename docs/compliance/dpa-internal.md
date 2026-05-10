# DPA Interno — Controlador / Operador

**Documento:** DPA-INT-001  
**Versão:** 1.0-rascunho  
**Data:** 2026-05-09  
**Classificação:** Confidencial

---

## 1. Partes

| Papel | Entidade |
|---|---|
| **Controlador** | \[Nome da empresa / app de telemedicina\] |
| **Operador** | \[Nome da empresa / componente assistente_de_voz\] |

Embora Controlador e Operador possam ser a mesma entidade jurídica, este DPA formaliza a
cadeia de responsabilidade exigida pelo Art. 39 da LGPD.

---

## 2. Base legal

O Controlador detém base legal **Art. 11 §2 II "a" da LGPD** (tutela da saúde) para
tratamento dos dados sensíveis (transcrições de anamnese, drafts de prontuário). O
Controlador instrui o Operador a processar esses dados exclusivamente para as finalidades
descritas na Seção 3.

---

## 3. Finalidades autorizadas

1. Transcrição de voz do paciente/profissional durante consulta.
2. Extração estruturada de draft de prontuário (campos de anamnese).
3. Detecção de red flags clínicos para orientação de segurança.
4. Registro de audit log para rastreabilidade e conformidade (5 anos, Art. 37 LGPD).

O Operador é **proibido** de usar os dados para qualquer outra finalidade sem instrução
escrita do Controlador.

---

## 4. Sub-operadores declarados

O Operador utiliza os seguintes sub-operadores para processar dados dos titulares:

| Sub-operador | Serviço | País de processamento | Mecanismo de transferência |
|---|---|---|---|
| Deepgram | STT (transcrição de voz) | EUA | Cláusula contratual padrão + DPA assinado (pendente) |
| OpenAI | LLM (extração de draft) | EUA | Cláusula contratual padrão + DPA assinado (pendente) |
| Anthropic | LLM fallback (Claude Sonnet) | EUA | Cláusula contratual padrão + DPA assinado (pendente) |

> **Atenção:** Os DPAs com Deepgram, OpenAI e Anthropic devem incluir cláusula de
> adequação para transferência internacional (Art. 33 LGPD). Nenhum dado de paciente real
> deve ser processado antes da assinatura desses DPAs.

---

## 5. Obrigações do Operador (Art. 39 LGPD)

- Processar dados apenas conforme instruções documentadas do Controlador.
- Garantir confidencialidade (acesso DBA restrito a ≤ 3 pessoas nomeadas).
- Notificar o Controlador em < 24h em caso de incidente de segurança (ver
  `docs/runbooks/breach-notification.md`).
- Cooperar com auditorias do Controlador mediante aviso prévio de 5 dias úteis.
- Eliminar ou devolver dados ao término do contrato conforme instrução do Controlador.

---

## 6. Retenção

| Tipo de dado | Retenção | Responsável pela eliminação |
|---|---|---|
| Áudio da sessão | Efêmero (não persistido pelo componente) | N/A |
| Transcrição / draft | 6–12 meses | Controlador instrui Operador |
| AuditLog | 5 anos | Operador (controlado pelo Controlador) |

---

## 7. Assinaturas

| Papel | Nome | Cargo | Data | Assinatura |
|---|---|---|---|---|
| Controlador | | | | |
| Operador | | | | |

> **[ ] DPA assinado** — obrigatório antes do primeiro paciente real.
