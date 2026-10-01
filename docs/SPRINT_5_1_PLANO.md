# Sprint 5.1 — Modelo de produtos, serviços e kits

Etapas A (diagnóstico) e B (modelagem) foram validadas antes da implementação,
com as recomendações aceitas e a decisão de que "entrega sem montagem" fica
só como atividade da Esteira. A seção **E. Entrega** no fim descreve o que foi
implementado.

## A. Diagnóstico da arquitetura atual

### Como as peças se relacionam hoje

```
categorias ─┬─ produtos ──┬─ fotos_produto, tags_produto
            │             └─ itens_kit (produto_id, quantidade INTEGER) ── kits
            └─ kits ─────────── fotos_kit, tags_kit

orcamentos ── itens_orcamento (tipo, item_id, descricao, quantidade, preco_unitario)
pedidos ───── itens_pedido    (tipo, item_id, descricao, quantidade, preco_unitario)
                tipo = 'produto' | 'kit' | 'servico'; item_id SEM chave estrangeira

servicos (nome, ativo, ordem)  ← lista de atividades da operação:
                                 Entrega, Retirada, Montagem, Desmontagem...
```

### O que existe e será reaproveitado

| Recurso | Situação | Reaproveitamento |
|---|---|---|
| `produtos` | Cadastro único, com estoque (`quantidade_total`), dias de uso, antecedência, foto, SEO, slug, vitrine | Passa a guardar os 3 tipos (locação, encomenda, serviço). Não cria tabela nova de "serviços cobrados". |
| `kits` + `itens_kit` | Kit com preço fechado; componentes só de `produtos`; soma das peças já calculada | Componentes continuam apontando para `produtos` — como serviços e balões também ficam em `produtos`, o kit misto não exige outra tabela. |
| `itens_pedido` / `itens_orcamento` | Cada linha já copia descrição e preço no momento da venda | A preservação de preço (item 6.3) **já funciona**; falta preservar a composição do kit (ver riscos). |
| Motor de disponibilidade (Sprint 7) | Estoque físico − pedidos em andamento; kit = peça mais escassa; histórico não conta | Continua igual, mas passa a considerar **só componentes de locação**. |
| `servicos` | Lista de atividades usada pela Esteira e Agenda para separar entrega/retirada | Mantida. Serviço cobrado (produto tipo serviço) aponta para a atividade correspondente. |
| Vitrine, administração em abas, auditoria, permissões | Prontos | Ganham campos por tipo, unidade e modalidades. |

### Impactos e riscos encontrados

1. **Todo produto é tratado como locação.** O motor de disponibilidade, a
   vitrine ("Disponível / Últimas unidades") e a validação do pedido exigem
   estoque de qualquer produto. Uma guirlanda cadastrada hoje teria de ter
   "estoque" para ser vendida. → Filtrar por tipo em `_ocupacoes`,
   `_necessidades`, `calendario_item` e `_faltas_de_estoque`.
2. **Quantidade só inteira.** As colunas são `INTEGER` e o código faz
   `int(quantidade)` em vários pontos (orçamento, pedido, vitrine, motor de disponibilidade).
   2,5 m de guirlanda viraria 2. → Trocar por um conversor único com precisão
   de 3 casas, válido por unidade de cobrança.
3. **Serviço é texto livre no pedido.** Montagem entra como descrição digitada,
   sem preço de referência nem vínculo. A Esteira decide "entrega x retirada"
   procurando palavras no nome do serviço. → Manter essa leitura (não quebra a
   operação) e acrescentar a modalidade explícita no pedido.
4. **Kit muda o passado.** O motor abre o kit pela composição **atual**: se
   alguém trocar as peças de um kit, os pedidos antigos passam a ocupar as
   peças novas. → Gravar a composição do kit na linha do pedido/orçamento no
   momento da venda e usá-la no motor.
5. **Sem cadastro de materiais.** Não existe tabela de insumos, receita ou
   movimentação. → Criar (aditivo).
6. **Sem dados reais conhecidos.** O banco local está vazio. A classificação
   dos produtos atuais depende do mapa da VPS (ferramenta abaixo).

## B. Modelo proposto (somente acréscimos)

Nenhuma coluna é removida ou renomeada; nenhum registro é apagado. O código
antigo ignora as colunas novas, então voltar a versão anterior funciona sem
mexer no banco.

### `produtos` — novas colunas

| Coluna | Tipo | Uso |
|---|---|---|
| `tipo` | TEXT, nulo = "a classificar" | `locacao`, `encomenda`, `servico` |
| `unidade` | TEXT, padrão `unidade` | `unidade`, `metro`, `servico`, `hora`, `pacote` |
| `qtd_minima` | REAL | Quantidade mínima por pedido |
| `prazo_producao_dias` | INTEGER | Encomenda: antecedência mínima de produção |
| `tempo_producao_min` / `tempo_execucao_min` | INTEGER | Estimativas (encomenda / serviço) |
| `permite_retirada`, `permite_montagem` | INTEGER 0/1 | Modalidades aceitas |
| `exige_agendamento`, `cobra_deslocamento` | INTEGER 0/1 | Serviço |
| `local_execucao` | TEXT | Serviço: `evento`, `loja` |
| `servico_id` | FK `servicos` | Serviço cobrado ↔ atividade da Esteira/Agenda |

Unidades permitidas por tipo (validado no servidor):
locação → unidade; encomenda → unidade ou metro; serviço → serviço, hora ou
pacote. Só `metro` (e `hora`) aceitam fração.

### `kits` / `itens_kit`

- `kits`: `codigo_sku`, `modo_preco` (`fechado` — comportamento atual — ou
  `componentes`), `permite_retirada`, `permite_montagem`.
- `itens_kit`: `quantidade` passa a aceitar fração (2,5 m), `obrigatorio`
  (padrão 1).
- **Kit dentro de kit: não permitido** (recomendação). Elimina composição
  circular e recursão por construção; a validação recusa no servidor.

### Materiais e receita (novas tabelas)

```
materiais           id, tenant_id, codigo, nome, unidade, custo_referencia,
                    arredondamento ('exato' | 'inteiro_acima'), ativo, datas
receitas_produto    id, produto_id → produtos, material_id → materiais,
                    quantidade (por 1 unidade de cobrança), observacao
                    UNIQUE (produto_id, material_id)
movimentos_material id, tenant_id, material_id, tipo ('entrada' | 'reserva' |
                    'baixa' | 'perda' | 'ajuste' | 'estorno'), quantidade,
                    pedido_id, item_pedido_id, usuario_id, observacao, criado_em
```

- **Consumo previsto não é gravado**: é calculado (receita × quantidade, com o
  arredondamento de cada material) sempre que pedido.
- No 5.1 nenhuma movimentação é gerada automaticamente: consultar, orçar ou
  adicionar à lista da vitrine nunca baixa nem reserva material. A tabela fica
  pronta para o Sprint 6 (reserva na confirmação, baixa na produção) e aceita
  ajustes manuais auditados.

### Pedidos e orçamentos

- `pedidos.modalidade` e `orcamentos.modalidade`: `retirada` | `montagem`.
  Pedidos antigos ficam **vazios** (não deduzidos) e continuam funcionando
  pela leitura atual dos serviços.
- `itens_pedido` / `itens_orcamento`: `unidade` (cópia na venda) e
  `composicao` (JSON da composição do kit na venda). Quantidade fracionada
  usa a coluna existente (o SQLite guarda 2,5 sem perda); não há recriação de
  tabela.

### Migração dos produtos existentes

1. Backup automático do arquivo do banco antes de migrar (como no Sprint 2.1).
2. Todos os produtos atuais ficam com `tipo` **vazio = "A classificar"** e
   `unidade = unidade`. Enquanto vazio, o item se comporta **exatamente como
   hoje** (locação com estoque), então nada muda na operação.
3. A administração mostra a aba "A classificar", com a sugestão da ferramenta
   e confirmação individual ou em lote. Nenhum produto é classificado sozinho.
4. Kits atuais: `modo_preco = fechado` (preço de hoje), retirada e montagem
   permitidas, todas as peças obrigatórias.
5. Verificação antes/depois: contagem e soma de preços de produtos, kits,
   itens de kit, pedidos e orçamentos precisam bater.

Reversão: restaurar o backup gerado no passo 1, ou simplesmente voltar o
código (as colunas novas são ignoradas pela versão anterior).

## C. Implementação prevista (após validação)

1. Migração aditiva + verificação antes/depois.
2. Regras centrais no backend: unidades por tipo, conversor de quantidade,
   preço de referência do kit, consumo previsto, compatibilidade de modalidade.
3. Motor de disponibilidade: só locação ocupa estoque; encomenda respeita o
   prazo de produção; serviço não bloqueia (agenda da equipe = Sprint 6);
   pedidos novos usam a composição gravada.
4. Telas (Figma QruRvNueNTMCSkE0S5JAS1): listagem com filtro e selo por tipo;
   cadastro com campos por tipo; receita de materiais; composição e
   precificação do kit; cadastro de materiais.
5. Orçamento/pedido: quantidade fracionada, unidade ao lado do preço,
   modalidade com validação.
6. API JSON para preço de referência do kit e consumo previsto.
7. Testes dos critérios de aceite + regressão completa.

## Pendências para o Sprint 6

Saldo de materiais e reserva/baixa automáticas; capacidade e agenda da
equipe de montagem; capacidade de produção por dia; manutenção de peças;
disponibilidade conjunta usando os três controles.

## Decisões tomadas

1. Produtos atuais ficam "A classificar" (funcionando como locação) até a
   confirmação na tela — individual ou em lote, com sugestão pelo nome.
2. Kit não contém outro kit (componentes são sempre produtos).
3. Modalidades: só **retirada** e **montagem no local**. "Entrega" continua
   como atividade da Esteira.
4. Componentes obrigatórios e opcionais. Opcional não entra no preço de
   referência do pacote, não ocupa estoque e é vendido à parte.
5. Arredondamento definido por material (inteiro para cima ou exato).

## E. Entrega

### O que foi implementado

| Área | Onde |
|---|---|
| Regras centrais (tipos, unidades, quantidade, consumo, preço do kit, modalidades, sugestão de tipo) | `sistema/regras_itens.py` |
| Migração aditiva com backup e conferência antes/depois | `dados._migrar_sprint51`, `dados.backup_antes_sprint51` |
| Cadastro por tipo, materiais, receita, movimentações manuais | `dados.salvar_produto`, `salvar_material`, `salvar_linha_receita`, `registrar_movimento_material` |
| Kits mistos, preço por componentes ou fechado, obrigatório/opcional | `dados.adicionar_item_kit`, `resumo_kit`, `_sincronizar_precos_kits` |
| Composição do kit gravada na venda | `itens_pedido.composicao` / `itens_orcamento.composicao` (`_preparar_linhas`) |
| Disponibilidade por tipo (locação = estoque; encomenda = prazo; serviço = não bloqueia) | `dados.calendario_item`, `_ocupacoes`, `_necessidades` |
| Modalidade no pedido e no orçamento, com validação | `pedidos.modalidade`, `orcamentos.modalidade`, `_validar_modalidade_venda` |
| Telas | `item_catalogo.html` (tipo, produção, execução, modalidades, Receita, Componentes), `catalogo_admin.html` (selo de tipo, filtro, aba "A classificar"), `materiais.html`, `material.html`, `pedido.html`/`orcamento.html` (modalidade, fração, busca no catálogo), `pedido_festas.html` (materiais previstos), vitrine (preço por unidade e metros) |
| Testes de aceite | `tests/test_sprint51.py` (48 testes) |

### API (JSON, exige login)

| Método e rota | Permissão | Retorno |
|---|---|---|
| `GET /api/catalogo/itens?q=&natureza=&aba=` | catalog.view | Lista de produtos e kits (tipo, unidade, preço, status) |
| `GET /api/catalogo/produto/<id>` | catalog.view | Produto com tipo, modalidades e receita |
| `GET /api/catalogo/produto/<id>/consumo?quantidade=2.5` | catalog.view | Consumo previsto por material e custo; 422 se a quantidade não serve para a unidade |
| `GET /api/catalogo/kit/<id>/preco` | catalog.view | Componentes, referência (obrigatórios/opcionais), preço final, diferença, modalidades e restrições |
| `GET /api/pedido/<id>/consumo` | orders.view | Materiais previstos para o pedido (base da reserva do Sprint 6) |

Rotas de tela novas: `/materiais`, `/material/<id>`, `/material/<id>/movimento`,
`/produto/<id>/receita`, `/produto/<id>/receita/<linha>/remover`,
`/catalogo-admin/classificar`. `/kit/<id>/item` passou a aceitar quantidade
fracionada e `obrigatorio` (1/0). Erros seguem o padrão do sistema: mensagem
ao lado do campo nas telas e `{"ok": false, "campo", "mensagem"}` na API.

### Permissões

Comercial cadastra e edita itens, kits e receitas; estoque físico, dias de
uso, antecedência e **prazo de produção** só com `inventory.edit`; ativar e
inativar com `catalog.status`; materiais são vistos por quem vê o catálogo e
alterados só com `inventory.edit`.

### Limitações e pendências para o Sprint 6

- Saldo de materiais: só lançamentos manuais (entrada, perda, ajuste). Reserva
  na confirmação do pedido, baixa na produção e estorno no cancelamento usam a
  tabela `movimentos_material` (tipos já previstos) e `consumo_previsto_pedido`.
- Encomenda: a disponibilidade considera só o prazo de produção; falta cruzar
  com o saldo de materiais e a capacidade diária de produção.
- Serviço: não bloqueia datas; falta agenda e capacidade da equipe (o vínculo
  `servico_id` com a atividade da Esteira já existe).
- Opcionais do kit são vendidos como linha à parte no pedido; escolher
  opcionais dentro da linha do kit fica para quando houver essa necessidade.
- O pedido e o orçamento continuam com itens de texto livre; a busca no
  catálogo liga a linha ao cadastro (tipo, unidade, preço sugerido).
- Figma QruRvNueNTMCSkE0S5JAS1: o conector estava sem autorização; as telas
  seguem o padrão visual já usado no sistema.
