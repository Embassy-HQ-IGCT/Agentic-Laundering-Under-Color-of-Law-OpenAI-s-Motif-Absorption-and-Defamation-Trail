// SPDX-License-Identifier: Apache-2.0
// Rafiki end-to-end: asset -> liquidity -> 2 wallet addresses -> receiver -> quote -> outgoing payment -> settled + webhooks.
// Run from rafiki/packages/backend (so `canonicalize` resolves):  node ../../../rafiki-e2e.cjs
const { createHmac, randomUUID } = require('crypto')
const { canonicalize } = require(require.resolve('json-canonicalize', { paths: [process.cwd()] }))
const fs = require('fs'), path = require('path')
const SECRET = 'iyIgCprjb9uL8wFckR+pLEkJWMB7FJhgkvqhTQR/964=', TENANT = '438fa74a-fa7d-4317-9ced-dde32ece1787'
const URL_ = process.env.ADMIN_URL || 'http://localhost:3001/graphql'
const WEBHOOKS = process.env.WEBHOOK_LOG || path.resolve(__dirname, 'rafiki-run/webhooks.jsonl')
let pass = 0, fail = 0
const ok = (n) => { console.log('PASS ', n); pass++ }, ko = (n, w) => { console.log('FAIL ', n, '->', w); fail++ }
async function gql(query, variables) {
  const body = { query, variables }, ts = Date.now()
  const sig = createHmac('sha256', SECRET).update(`${ts}.${canonicalize(body)}`).digest('hex')
  const r = await fetch(URL_, { method: 'POST', headers: { 'content-type': 'application/json', signature: `t=${ts}, v1=${sig}`, 'tenant-id': TENANT }, body: JSON.stringify(body) })
  const j = await r.json(); if (j.errors) throw new Error(JSON.stringify(j.errors).slice(0, 300)); return j.data
}
const { execFileSync } = require('child_process')
const CTL = process.env.LEDGERCTL, LEDGER = process.env.LEDGER_NAME || 'rafiki'
const { spawnSync } = require('child_process')
const ctl = (...a) => { const r = spawnSync(CTL, ['--insecure', '--server=localhost:8888', ...a], { encoding: 'utf8' }); return (r.stdout || '') + (r.stderr || '') + (r.status ? ` [exit ${r.status}]` : '') }
const bal = (acct) => { const out = ctl('accounts', 'get', acct, '--ledger', LEDGER, '--json'); try { const v = JSON.parse(out.slice(out.indexOf('{'))).volumes.find((x) => x.asset === 'USD/2'); return v ? Number(v.volumes.balance) : 0 } catch { return null } }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
;(async () => {
  const run = Date.now().toString(36), code = 'USD'
  const existing = (await gql(`query{assets(first:100){edges{node{id code scale}}}}`)).assets.edges.map((e) => e.node).find((n) => n.code === code && n.scale === 2)
  const a = { createAsset: { asset: existing || (await gql(`mutation($i:CreateAssetInput!){createAsset(input:$i){asset{id code scale}}}`, { i: { code, scale: 2, idempotencyKey: randomUUID() } })).createAsset.asset } }
  const asset = a.createAsset.asset; ok(`create asset ${asset.code}/${asset.scale}`)
  const dep = await gql(`mutation($i:DepositAssetLiquidityInput!){depositAssetLiquidity(input:$i){success}}`, { i: { assetId: asset.id, amount: '100000', id: randomUUID(), idempotencyKey: randomUUID() } })
  dep.depositAssetLiquidity.success ? ok('deposit 1000.00 USD liquidity') : ko('deposit liquidity', JSON.stringify(dep))
  const mk = async (n) => (await gql(`mutation($i:CreateWalletAddressInput!){createWalletAddress(input:$i){walletAddress{id address}}}`, { i: { assetId: asset.id, address: `${n}${run}`, publicName: n, idempotencyKey: randomUUID() } })).createWalletAddress.walletAddress
  const alice = await mk('alice'), bob = await mk('bob'); ok(`wallet addresses ${alice.address}, ${bob.address}`)
  if (CTL) { const f = ctl('tx', 'create', '--ledger', LEDGER, '--posting', `world,customers:${alice.id},5000,USD/2`); /SUCCESS/.test(f) ? ok('ledger: alice funded 50.00 USD (world -> customers:alice)') : ko('fund alice in ledger', f.slice(0, 200)) }
  const waj = await new Promise((res, rej) => require('http').get({ host: 'localhost', port: 3000, path: new URL(alice.address).pathname, headers: { accept: 'application/json', host: 'cloud-nine.local' } }, (r) => { let d = ''; r.on('data', (c) => (d += c)); r.on('end', () => { try { res(JSON.parse(d)) } catch (e) { rej(e) } }) }).on('error', rej))
  waj.assetCode === 'USD' && waj.authServer ? ok('Open Payments wallet-address document served (assetCode, authServer)') : ko('wallet address doc', JSON.stringify(waj).slice(0, 200))
  // fund alice: deposit into her wallet address account via liquidity? Use sender-side: Rafiki checks balance at funding; fund via webhook-style deposit
  const recv = (await gql(`mutation($i:CreateReceiverInput!){createReceiver(input:$i){receiver{id completed}}}`, { i: { walletAddressUrl: bob.address, incomingAmount: { value: '1500', assetCode: 'USD', assetScale: 2 }, idempotencyKey: randomUUID() } })).createReceiver.receiver
  ok(`receiver (incoming payment) ${recv.id.split('/').pop()}`)
  const q = (await gql(`mutation($i:CreateQuoteInput!){createQuote(input:$i){quote{id debitAmount{value} receiveAmount{value}}}}`, { i: { walletAddressId: alice.id, receiver: recv.id, receiveAmount: { value: '1500', assetCode: 'USD', assetScale: 2 }, idempotencyKey: randomUUID() } })).createQuote.quote
  q.receiveAmount.value === '1500' ? ok(`quote debit=${q.debitAmount.value} receive=${q.receiveAmount.value}`) : ko('quote', JSON.stringify(q))
  const op = (await gql(`mutation($i:CreateOutgoingPaymentInput!){createOutgoingPayment(input:$i){payment{id state}}}`, { i: { walletAddressId: alice.id, quoteId: q.id, idempotencyKey: randomUUID() } })).createOutgoingPayment.payment
  ok(`outgoing payment created state=${op.state}`)
  // Outgoing payment is FUNDING until the ASE deposits sender liquidity (outgoing_payment.created webhook).
  const hooks = () => fs.existsSync(WEBHOOKS) ? fs.readFileSync(WEBHOOKS, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) : []
  let created; for (let i = 0; i < 20 && !created; i++) { created = hooks().find((h) => h.type === 'outgoing_payment.created' && h.data.id === op.id); if (!created) await sleep(1000) }
  created ? ok('webhook outgoing_payment.created received by ASE') : ko('webhook outgoing_payment.created', 'not received')
  if (created) {
    const dp = await gql(`mutation($i:DepositOutgoingPaymentLiquidityInput!){depositOutgoingPaymentLiquidity(input:$i){success}}`, { i: { outgoingPaymentId: op.id, idempotencyKey: randomUUID() } })
    dp.depositOutgoingPaymentLiquidity.success ? ok('ASE funds outgoing payment (liquidity deposit)') : ko('fund payment', JSON.stringify(dp))
  }
  let st; for (let i = 0; i < 30; i++) { st = (await gql(`query($id:String!){outgoingPayment(id:$id){state error}}`, { id: op.id })).outgoingPayment; if (['COMPLETED', 'FAILED', 'CANCELLED'].includes(st.state)) break; await sleep(1000) }
  st.state === 'COMPLETED' ? ok('outgoing payment COMPLETED (ILP send/settle)') : ko('payment completion', JSON.stringify(st))
  let types = []; for (let i = 0; i < 15; i++) { types = hooks().map((h) => h.type); if (types.includes('incoming_payment.completed') && types.includes('outgoing_payment.completed')) break; await sleep(1000) }
  for (const t of ['incoming_payment.created', 'outgoing_payment.created', 'outgoing_payment.completed', 'incoming_payment.completed']) types.includes(t) ? ok(`webhook ${t}`) : ko(`webhook ${t}`, 'missing')
  if (CTL) {
    await sleep(1500)
    const [ba, bb, bs, bi] = [bal(`customers:${alice.id}`), bal(`customers:${bob.id}`), bal('rafiki:settlement'), bal(`rafiki:inflight:${op.id}`)]
    ba === 3500 ? ok('ledger: alice balance 3500 (5000 - 1500)') : ko('ledger alice', ba)
    bb === 1500 ? ok('ledger: bob balance 1500') : ko('ledger bob', bb)
    bs === 0 ? ok('ledger: rafiki:settlement nets to 0') : ko('ledger settlement', bs)
    bi === 0 ? ok('ledger: in-flight reservation cleared') : ko('ledger inflight', bi)
  }
  console.log(`\nRESULT: ${pass} passed, ${fail} failed`); process.exit(fail ? 1 : 0)
})().catch((e) => { console.error('ERROR', e.message); process.exit(2) })
