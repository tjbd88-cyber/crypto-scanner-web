/* Server owns scheduling and settings. This timer only refreshes displayed status. */
(() => {
  const el = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {binance:'Binance',okx:'OKX',gate:'Gate',spot:'现货',perpetual:'永续'};
  let loaded=false, offset=0, rows=[], busy=false;
  const time = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '—';
  async function request(url,body) {
    const options = body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
    if(body !== undefined && el('auto-token').value) options.headers.Authorization='Bearer '+el('auto-token').value;
    const response = await fetch(url,options);
    const data = await response.json();
    if(!response.ok) throw Error(typeof data.detail==='string'?data.detail:`参数或服务错误（HTTP ${response.status}）`);
    return data;
  }
  async function status() {
    try {
      const data=await request('/api/auto-scan/status');
      el('auto-state').textContent=data.running?'运行中':data.enabled?'已开启':'已关闭';
      if(!loaded) {
        for(const group of ['exchanges','markets']) for(const box of el('auto-'+group).querySelectorAll('input')) box.checked=data[group].includes(box.value);
        el('auto-turnover').value=data.min_previous_day_turnover;
        el('auto-limit').value=data.max_symbols||'';
        loaded=true;
      }
      el('auto-warning').textContent=(data.warnings||[]).join(' ');
      el('auto-next').textContent=data.next_run?time(data.next_run):data.enabled&&data.scheduler_mode==='external'?'等待外部调度':'—';
      const run=data.last_run, result=data.last_result||{};
      el('auto-last').textContent=time(run?.started_at);
      el('auto-duration').textContent=run?.duration!==undefined?run.duration+' 秒':'—';
      el('auto-new').textContent=result.new_signals||0;
      const states={running:'运行中',completed:'完成',completed_with_warnings:'部分完成',skipped:'跳过',failed:'失败',stopped:'已停止'};
      el('auto-summary').textContent=run?`${states[run.status]||run.status} · 分析 ${result.analyzed||0} · 无新K线 ${result.no_new_candle||0} · 基线 ${result.baseline_signals||0} · 已有信号 ${result.existing_signals||0} · 失败 ${run.stats?.failed_symbols||0} · 限流市场 ${result.rate_limited||0}${run.reason?' · '+run.reason:''}`:'尚未运行自动扫描';
      const markets={pending:'等待',filtering:'成交额筛选',scanning:'扫描中',success:'正常',completed_with_warnings:'部分完成',rate_limited:'受限',unavailable:'不可用',stopped:'已停止'};
      el('auto-market-status').innerHTML=Object.values(run?.market_stats||{}).map(m=>`<div class="market-state ${escape(m.status)}"><strong>${escape(labels[m.exchange])} ${escape(labels[m.market])}<span>${escape(markets[m.status]||m.status)}</span></strong><small>处理 ${m.processed_symbols||0} · 失败 ${m.failed_symbols||0} · 跳过 ${m.skipped_symbols||0}</small></div>`).join('');
    } catch(e) {el('auto-state').textContent='暂时无法读取';el('auto-error').textContent=e.message;}
  }
  async function action(path,body={}) {
    if(busy)return;
    busy=true;el('auto-error').textContent='';
    try {
      const data=await request(path,body);
      if(data.reason)el('auto-error').textContent='本轮跳过：'+data.reason;
      await status();
    }catch(e){el('auto-error').textContent=e.message;}
    finally{busy=false;}
  }
  el('auto-enable').addEventListener('click',()=>action('/api/auto-scan/enable',{
    exchanges:[...el('auto-exchanges').querySelectorAll('input:checked')].map(x=>x.value),
    markets:[...el('auto-markets').querySelectorAll('input:checked')].map(x=>x.value),periods:['4h'],interval_seconds:3600,
    min_previous_day_turnover:Number(el('auto-turnover').value),max_symbols:el('auto-limit').value?Number(el('auto-limit').value):null}));
  el('auto-disable').addEventListener('click',()=>action('/api/auto-scan/disable'));
  el('auto-check').addEventListener('click',()=>action('/api/internal/auto-scan'));
  async function history() {
    try {
      const query=new URLSearchParams({limit:'100',offset:String(offset)});
      for(const [id,name] of [['exchange','exchange'],['market','market'],['period','period'],['pair','pair'],['stage','stage'],['from','date_from'],['to','date_to']])if(el('history-'+id).value)query.set(name,el('history-'+id).value);
      const data=await request('/api/signals?'+query);
      rows=data.items;el('history-count').textContent=data.total+' 条';
      el('history-page').textContent=`第 ${Math.floor(offset/100)+1} 页`;
      el('history-prev').disabled=offset===0;el('history-next').disabled=offset+100>=data.total;
      el('history-results').innerHTML=rows.length?rows.map((r,i)=>`<tr class="result-row" data-history-index="${i}" tabindex="0"><td><b>${escape(r.pair)}</b><small>${escape(labels[r.exchange])} · ${escape(labels[r.market])}</small></td><td>${escape(r.period.toUpperCase())}<small>${escape(r.status==='invalid'?'失效（原 '+r.stage+'）':r.stage)}</small><small>${escape(r.pattern_type||'旧版历史规则')}</small><small>${escape([r.strength,r.freshness].filter(Boolean).join(' · '))}</small></td><td title="${escape(Object.entries(r.score_breakdown||{}).map(([k,v])=>`${k} ${v}`).join(' · '))}">${r.score}</td><td>${escape(r.candle_time)}<small>${escape(time(r.detected_at))}</small></td><td>${r.decline_bars?`${r.decline_bars} 根<small>-${r.decline_pct}%</small>`:'横盘 / 蓄势'}</td><td>SAR ${r.sar_flip_bars_ago===null?'待翻多':r.sar_flip_bars_ago+'根前'}<small>HA ${r.ha_flip_bars_ago===null?'待翻阳':r.ha_flip_bars_ago+'根前'}</small></td><td class="reason">${r.reasons.map(escape).join(' · ')}</td></tr>`).join(''):'<tr><td colspan="7" class="empty">当前筛选范围没有历史信号；首次基线结果会静默保存。</td></tr>';
      el('history-error').textContent='';
    }catch(e){el('history-error').textContent='历史读取失败：'+e.message;}
  }
  el('history-filter').addEventListener('click',()=>{offset=0;history();});
  el('history-prev').addEventListener('click',()=>{offset=Math.max(0,offset-100);history();});
  el('history-next').addEventListener('click',()=>{offset+=100;history();});
  function open(event) {const row=event.target.closest('[data-history-index]');if(row&&window.openResultChart)window.openResultChart(rows[Number(row.dataset.historyIndex)]);}
  el('history-results').addEventListener('click',open);
  el('history-results').addEventListener('keydown',e=>{if(e.key==='Enter')open(e);});
  status();history();setInterval(()=>{status();history();},15000);
})();
