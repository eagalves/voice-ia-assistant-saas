# Runbook — Notificação de Breach de Dados

**Documento:** RUN-BREACH-001  
**Versão:** 1.0  
**Classificação:** Confidencial — Restrito a equipe de segurança e DPO

---

## Objetivo

Descrever os passos obrigatórios para notificação de incidente de segurança envolvendo
dados pessoais (ou sensíveis) processados pelo componente `assistente_de_voz`, conforme
Art. 39 e Art. 48 da LGPD.

---

## Definição de breach

Qualquer evento que comprometa a **confidencialidade**, **integridade** ou
**disponibilidade** de dados de titulares processados pelo componente, incluindo mas não
limitado a:

- Acesso não autorizado ao banco de dados (transcrições, drafts, audit log).
- Vazamento de áudio ou transcrição para terceiros não autorizados.
- Comprometimento de credenciais de API (Deepgram, OpenAI, Anthropic).
- Reescrita ou adulteração do AuditLog detectada pela verificação do hash chain.
- Exposição acidental de dados de pacientes em logs, métricas ou respostas de API.

---

## Fluxo de notificação

### Fase 1 — Detecção e contenção (< 1h)

1. **Detectar e registrar** o incidente com timestamp, origem, sistemas afetados e dados
   potencialmente expostos.
2. **Isolar** o componente comprometido (desligar serviço / revogar credenciais conforme
   necessário).
3. **Preservar evidências** — logs, snapshots do AuditLog, registros de acesso. NÃO
   apagar nada antes da análise forense.
4. **Acionar** a equipe de segurança e o DPO imediatamente (Slack #incidentes-seguranca +
   e-mail de emergência).

### Fase 2 — Avaliação de impacto (< 4h)

5. Identificar quais tenants e titulares foram afetados.
6. Determinar tipo de dado exposto (áudio, transcrição, draft, metadados).
7. Avaliar risco de dano real aos titulares (Art. 48 §1 LGPD: "risco ou dano relevante").
8. Documentar no Registro de Incidentes (planilha/sistema interno).

### Fase 3 — Notificação ao Controlador (< 24h do conhecimento)

9. Notificar o Controlador por escrito (e-mail com confirmação de leitura) contendo:
   - Data/hora de detecção do incidente.
   - Natureza do incidente.
   - Dados e titulares afetados (estimativa).
   - Medidas de contenção já adotadas.
   - Plano de remediação com prazos.

> **Prazo legal:** Art. 39 LGPD + DPA interno — o Operador tem **< 24h** para notificar
> o Controlador após tomar conhecimento do incidente.

### Fase 4 — Notificação à ANPD (< 72h do conhecimento — se obrigatório)

10. O **Controlador** avalia se o incidente deve ser reportado à ANPD (Art. 48 LGPD).
    Critério: risco ou dano relevante aos titulares.
11. Se sim, o Controlador notifica a ANPD via sistema SEI/ANPD usando o template abaixo.
12. O Operador fornece suporte técnico ao Controlador para preenchimento do formulário.

#### Template ANPD (referência — Art. 48 LGPD)

```
NOTIFICAÇÃO DE INCIDENTE DE SEGURANÇA — LGPD Art. 48

Data/hora do incidente: _______________
Data/hora do conhecimento: _______________
Data desta notificação: _______________

Controlador: [Nome / CNPJ]
DPO: [Nome / e-mail / telefone]

Descrição do incidente:
[Descrever o que ocorreu, sistemas afetados, vetor de ataque se conhecido]

Dados e titulares afetados:
- Categoria de dado: [ex: dados de saúde — transcrições de anamnese]
- Número estimado de titulares: ___
- Número estimado de registros: ___

Consequências prováveis:
[Descrever riscos de dano para os titulares]

Medidas adotadas ou propostas:
[Contenção, remediação, comunicação aos titulares se aplicável]

Comunicação aos titulares: [ ] Sim  [ ] Não  [ ] Em avaliação
Justificativa se não: _______________
```

---

## Pós-incidente (< 30 dias)

- [ ] Relatório de incidente completo (root cause, timeline, impacto real).
- [ ] Revisão das medidas de segurança para evitar recorrência.
- [ ] Atualização do DPIA se novos riscos foram identificados.
- [ ] Registro no Registro de Operações de Tratamento (Art. 37 LGPD).
- [ ] Comunicação formal de encerramento ao DPO e ao Controlador.

---

## Contatos de emergência

| Papel | Nome | Contato |
|---|---|---|
| DPO | \[a preencher\] | \[e-mail / telefone\] |
| Responsável técnico | \[a preencher\] | \[e-mail / telefone\] |
| Representante do Controlador | \[a preencher\] | \[e-mail / telefone\] |

---

## Histórico de revisões

| Versão | Data | Responsável | Descrição |
|---|---|---|---|
| 1.0 | 2026-05-09 | Equipe técnica | Versão inicial |
