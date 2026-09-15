# Morumbi Festas — Sprints Desenvolvidos

Sistema de gestao para locacao de decoracao de festas.
Morumbi Festas, Campo Grande/MS.

**Stack:** Flask / SQLite / Jinja2 / Gunicorn  
**Porta:** 5001  
**Dominio:** morumbifestas.duckdns.org  
**VPS:** 23.95.96.132 (Caddy como reverse proxy)  
**Perfis:** admin, comercial, operacional, gestor

---

## S01 — Fundacao

Estrutura base do sistema completo.

- **Flask + SQLite** com WAL e foreign keys
- **Autenticacao** com senha hash (werkzeug), sessao Flask
- **Perfis de acesso**: admin, comercial, operacional, gestor
- **Decoradores** `@exige_login` e `@exige_perfil` para protecao de rotas
- **Seed de admin** automatico na primeira execucao (via `FESTAS_SENHA`)
- **CRUD de usuarios**: nome, login, perfil, ativo/inativo
- **Audit log**: toda acao relevante (login, criacao, alteracao) registrada com usuario, tipo, descricao e timestamp
- **Dashboard** com tela de boas-vindas quando o banco esta vazio
- **Menu lateral** por grupos (Painel, Comercial, Catalogo, Administracao)
- **Templates base** com `base.html`, `_erro.html`, `_topo.html`
- **Filtros Jinja2**: `|dinheiro`, `|numero`, `|data`, `|datahora`, `|whatsapp_link`
- **Fuso horario** UTC-4 (Campo Grande) aplicado em toda gravacao e exibicao
- **Implantacao**: servico systemd, guia `IMPLANTAR.txt` passo a passo
- **Modulo `listas.py`**: ordenacao e busca reutilizaveis por toda aplicacao

**Testes:** 32  
**Arquivos:** `app.py`, `dados.py`, `auth.py`, `formato.py`, `listas.py`, `conftest.py`

---

## S02 — Clientes

Cadastro completo de clientes com protecoes de dados.

- **Campos**: nome, CPF/CNPJ, WhatsApp, telefone, email, data de nascimento, endereco, bairro, cidade, CEP, Instagram, observacoes, origem, status
- **Validacao de duplicidade** em CPF, WhatsApp e email (com `ErroDeCampo`)
- **Tags por cliente** (tabela separada `tags_cliente`, relacao N:N)
- **Filtros na lista**: por origem, por tag, busca textual, ordenacao por nome/cidade/data
- **Status**: ativo / inativo, com filtro "ativos", "inativos", "todos"
- **Origens de cliente**: Instagram, WhatsApp, Indicacao, Google, Facebook, Evento, Loja fisica, Outro
- **Exportacao CSV** — campos seguros apenas (nome, WhatsApp, cidade, bairro, origem, Instagram, tags, status). **CPF, email e endereco excluidos deliberadamente** por seguranca
- **Limpeza automatica** de documentos (so digitos) e telefones (digitos + "+")
- **Indices no banco** para CPF, WhatsApp e email

**Testes acumulados:** 62  
**Telas:** `clientes.html` (lista), `cliente.html` (formulario)

---

## S03 — Produtos e Categorias

Catalogo interno de produtos para locacao.

- **Categorias em arvore**: pai/filho com `categorias_arvore()`, exclusao protegida (em uso ou com filhos)
- **SKU automatico**: prefixo das 3 primeiras letras da categoria + sequencial (`MES-0001`, `CAD-0001`)
- **Campos do produto**: nome, categoria, descricao, preco de locacao, valor de referencia, quantidade total, status, localizacao, observacoes
- **Status**: disponivel, manutencao, inativo
- **Publicacao**: flag `publicado` para controlar visibilidade no catalogo publico
- **Fotos de produto**: upload com validacao de extensao (JPG, PNG, WebP), limite de 10 MB, redimensionamento automatico (max 1200px via Pillow), foto de capa
- **Tags por produto** (tabela `tags_produto`, relacao N:N)
- **Filtros na lista**: por categoria, por tag, busca textual, ordenacao por nome/SKU/preco/quantidade

**Testes acumulados:** 101  
**Telas:** `produtos.html` (lista), `produto.html` (formulario), `categorias.html` (arvore)

---

## S04 — Kits de Locacao

Agrupamento de produtos em kits com precificacao propria.

- **Kit**: nome, descricao, preco, status (ativo/inativo), publicado
- **Itens do kit**: vinculo produto-kit com quantidade, restricao de unicidade (mesmo produto nao repete no kit)
- **Soma de produtos**: calculo automatico do valor somado dos itens vs. preco do kit (exibe economia)
- **Fotos de kit**: mesmo sistema de upload/capa dos produtos
- **Disponibilidade do kit**: menor disponibilidade entre os produtos que compoe o kit (gargalo)
- **Produto em manutencao ou inativo** dentro do kit zera a disponibilidade
- **Filtros**: busca por nome/descricao, ordenacao por nome/preco, status

**Testes acumulados:** 131  
**Telas:** `kits.html` (lista), `kit.html` (formulario com itens e fotos)

---

## S05 — Catalogo Publico e Interno

Duas vitrines: uma publica sem login, outra interna para equipe.

### Catalogo publico (`/catalogo`)
- Acesso **sem autenticacao** — qualquer visitante ve
- Mostra apenas produtos e kits com `publicado = 1` e status ativo/disponivel
- Filtro por **categoria** e **busca textual**
- **Pagina de detalhe** do produto com galeria de fotos e informacoes
- **Pagina de detalhe** do kit com lista de produtos inclusos
- **Botao WhatsApp** para contato direto (via dados da organizacao)
- **Meta tags OG** para compartilhamento em redes sociais

### Catalogo interno (`/catalogo-interno`)
- Exige login (`@exige_login`)
- Mostra todos os produtos disponiveis e kits ativos, independente de publicacao
- Mesmos filtros de categoria e busca

### Organizacao
- Tabela `organizacoes`: nome, CNPJ, telefone, WhatsApp, email, endereco, Instagram
- Dados usados pelo catalogo publico (botao WhatsApp, rodape)

**Testes acumulados:** ~176  
**Telas:** `catalogo.html`, `catalogo_produto.html`, `catalogo_kit.html`, `catalogo_base.html`, `catalogo_interno.html`

---

## S06 — Comercial

Funil completo: lead -> orcamento -> pedido.

### Origens de lead
- CRUD de origens (`origens_lead`): cadastro, edicao, exclusao protegida (em uso)
- Sementes iniciais: Instagram, Facebook, WhatsApp, Google, Indicacao, Site, Recorrente

### Leads
- **Campos**: cliente, origem, interesse, valor estimado, responsavel, status, data do evento, observacoes
- **Etapas do funil**: novo, atendimento, orcamento, negociacao, reserva, contratado, concluido
- **Etapas alternativas**: perdido, cancelado
- **Visualizacao kanban** com cartoes por etapa e contadores
- **Visualizacao em lista** com busca e filtro por origem
- **Mover lead** entre etapas (drag-and-drop conceitual via botoes)

### Orcamentos
- **Campos**: cliente, lead vinculado, desconto, observacoes, status
- **Status**: rascunho, enviado, aceito, recusado
- **Itens dinamicos**: tipo (produto/kit), descricao, quantidade, preco unitario
- Calculo automatico de **subtotal** e **total** (com desconto)
- **Converter orcamento em pedido**: cria pedido com mesmos itens, marca orcamento como aceito, avanca lead para "contratado"

### Pedidos
- **Campos**: cliente, data do evento, status comercial, status operacional, observacoes
- **Status comercial**: confirmado, entregue, devolvido, cancelado
- **Status operacional**: preparacao, separado, montado, entregue, recolhido, conferido, cancelado
- **Cancelamento** com motivo registrado
- **Criacao direta** ou via conversao de orcamento

### Dashboard (atualizado)
- 6 contadores: clientes ativos, produtos, kits, leads em andamento, orcamentos abertos, pedidos ativos

**Testes acumulados:** 226  
**Telas:** `origens.html`, `leads.html`, `leads_kanban.html`, `lead.html`, `orcamentos.html`, `orcamento.html`, `pedidos.html`, `pedido.html`, `pedido_festas.html`

---

## S07 — Reservas e Disponibilidade

Controle de datas e bloqueio de overbooking.

### Datas no pedido
- **Data de retirada** e **data de devolucao** adicionadas ao pedido (migracao automatica em bancos existentes)
- Validacao: devolucao deve ser posterior a retirada

### Controle de disponibilidade
- **Calculo por periodo**: para cada produto, subtrai as unidades reservadas em pedidos que se sobrepoem ao periodo solicitado
- **Bloqueio de overbooking**: ao salvar pedido, se a quantidade solicitada excede a disponivel na data, `ErroDeCampo` impede a gravacao
- Pedido cancelado **nao consome** disponibilidade
- Edicao de pedido existente **exclui a si mesmo** do calculo (para poder editar sem conflito)

### Calendario de disponibilidade
- **Rota `/disponibilidade/<produto_id>`**: calendario mensal visual
- Navegacao mes a mes (anterior/proximo)
- Cada dia mostra total e disponivel para o produto
- Nomes dos meses em portugues

### API de disponibilidade
- **`/api/disponibilidade?produto_id=X&data_inicio=Y&data_fim=Z`**: retorna JSON com quantidade disponivel

### Cancelamento libera disponibilidade
- Cancelar pedido muda `status_comercial` e `status_operacional` para "cancelado"
- Pedido cancelado sai do calculo de reservas — unidades voltam a ficar disponiveis

### Disponibilidade de kit
- Calcula o minimo entre todos os produtos do kit no periodo
- Produto em manutencao ou inativo zera o kit

**Testes acumulados:** 258  
**Telas:** `disponibilidade.html` (calendario), `pedido.html` (atualizado com datas)

---

## Resumo Tecnico

| Sprint | Foco | Testes | Telas novas |
|--------|------|--------|-------------|
| S01 | Fundacao | 32 | 6 |
| S02 | Clientes | 62 | 2 |
| S03 | Produtos e categorias | 101 | 3 |
| S04 | Kits | 131 | 2 |
| S05 | Catalogo | ~176 | 5 |
| S06 | Comercial | 226 | 9 |
| S07 | Reservas | 258 | 1 |

### Banco de dados (13 tabelas)

1. `usuarios` — login, perfil, senha hash
2. `parametros` — chave/valor para configuracoes
3. `organizacoes` — dados da empresa
4. `audit_log` — rastreabilidade de acoes
5. `clientes` — cadastro completo
6. `tags_cliente` — tags livres por cliente
7. `categorias` — arvore pai/filho
8. `produtos` — catalogo de itens para locacao
9. `fotos_produto` — galeria com foto de capa
10. `tags_produto` — tags livres por produto
11. `kits` — agrupamento de produtos
12. `itens_kit` — composicao do kit
13. `fotos_kit` — galeria do kit
14. `origens_lead` — fontes de contato
15. `leads` — funil comercial
16. `orcamentos` — propostas com itens
17. `itens_orcamento` — itens do orcamento
18. `pedidos` — pedidos com datas de reserva
19. `itens_pedido` — itens do pedido

### Dependencias

- Flask >= 3.0
- Gunicorn >= 21.0
- Werkzeug >= 3.0
- Pillow >= 10.0

### Seguranca

- Senhas com hash (werkzeug)
- Controle de perfil por rota
- Exportacao CSV sem dados sensiveis (CPF, email, endereco)
- Variavel `FESTAS_SENHA` controla se autenticacao esta ativa
- Sem senha configurada: aviso visivel no dashboard, acesso livre
