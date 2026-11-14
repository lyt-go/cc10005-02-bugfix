import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/connector'
const columns = [
  "连接器编号",
  "协议类型",
  "目标端点",
  "认证参数",
  "重连次数",
  "心跳间隔",
  "数据编码",
  "超时阈值",
  "绑定代理"
]
const actions = [
  "发起连通测试",
  "标记就绪",
  "标记离线"
]
// 与后端状态机一致的动作可用范围：草稿→连通测试中→已就绪→已离线 单向推进
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  "草稿": ["发起连通测试"],
  "连通测试中": ["发起连通测试", "标记就绪", "标记离线"],
  "已就绪": ["标记离线"],
  "已离线": []
}

type Row = Record<string, string | number | null> & {
  id: number
  status?: string
  最近测试结果?: { 结论?: string; 依据?: string; 时间?: string } | null
  history?: Array<{ 时间?: string; 动作?: string; 结果?: string; 依据?: string; 说明?: string; 状态?: string }>
}

export default function ConnectorView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [busyKey, setBusyKey] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detail, setDetail] = useState<Row | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const refreshDetail = useCallback(async (id: number) => {
    const response = await request(`${ENDPOINT}/${id}`)
    if (!response.ok) {
      setDetail(null)
      return
    }
    setDetail((await response.json()) as Row)
  }, [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('数据连接器列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      // 详情面板与列表保持同一份新鲜数据：列表刷新后详情也重新拉取
      if (selectedId !== null) {
        await refreshDetail(selectedId)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '连接器管理列表读取失败')
    }
  }, [filters, selectedId, refreshDetail])

  useEffect(() => {
    void reload()
  }, [reload])

  async function openDetail(row: Row) {
    setSelectedId(row.id)
    setErrorMessage('')
    try {
      await refreshDetail(row.id)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '连接器详情读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    if (busyKey) {
      return
    }
    setBusyKey(`${row.id}:${action}`)
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok || !payload?.ok) {
        throw new Error(payload?.message ?? '连接器管理动作未生效，请稍后重试')
      }
      setNoticeMessage(payload.message ?? '操作已完成')
      // 动作完成后同时刷新列表与详情面板，避免两处各自显示旧值
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '连接器管理操作失败')
    } finally {
      setBusyKey('')
    }
  }

  const readyCount = rows.filter((row) => row.status === '已就绪').length
  const testingCount = rows.filter((row) => row.status === '连通测试中').length
  const stats = [
    { label: '连接器总数', value: total },
    { label: '已就绪连接器', value: readyCount },
    { label: '测试中连接器', value: testingCount },
  ]

  return (
    <section className="page" data-module="connector">
      <header className="page-head">
        <div>
          <h2>连接器管理管理</h2>
          <p className="page-desc">管理各类数据源的连接器配置，支持HTTP、数据库、消息队列等多种协议的统一接入。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('数据连接器登记入口尚未接入审批流')}>
            登记数据连接器
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出连接器管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const allowed = ACTIONS_BY_STATUS[String(row.status ?? '')] ?? []
            return (
              <tr key={String(row.id)} className={selectedId === row.id ? 'row-selected' : ''}>
                {columns.map((column) => (
                  <td key={column}>
                    <button className="link" type="button" onClick={() => void openDetail(row)}>
                      {row[column] ?? '—'}
                    </button>
                  </td>
                ))}
                <td><span className={`status-pill status-${String(row.status ?? '')}`}>{row.status ?? '—'}</span></td>
                <td className="row-actions">
                  {actions.map((action) => {
                    const enabled = allowed.includes(action) && !busyKey
                    const key = `${row.id}:${action}`
                    return (
                      <button
                        key={action}
                        className="link"
                        type="button"
                        disabled={!enabled}
                        title={allowed.includes(action) ? undefined : `当前状态「${row.status ?? '—'}」不允许该动作`}
                        onClick={() => void runAction(action, row)}
                      >
                        {busyKey === key ? '提交中…' : action}
                      </button>
                    )
                  })}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无连接器管理数据，可先登记数据连接器</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <aside className="detail-panel">
          <header className="detail-head">
            <h3>连接器详情：{String(detail['连接器编号'] ?? detail.id)}</h3>
            <button className="btn ghost" type="button" onClick={() => { setSelectedId(null); setDetail(null) }}>关闭</button>
          </header>
          <div className="detail-grid">
            <div><span className="detail-label">当前状态</span><strong>{detail.status ?? '—'}</strong></div>
            <div><span className="detail-label">目标端点</span><strong>{detail['目标端点'] ?? '—'}</strong></div>
            <div><span className="detail-label">绑定代理</span><strong>{detail['绑定代理'] || '未绑定'}</strong></div>
            <div>
              <span className="detail-label">最近测试结果</span>
              <strong>
                {detail.最近测试结果
                  ? `${detail.最近测试结果.结论 ?? '—'}${detail.最近测试结果.依据 ? `（${detail.最近测试结果.依据}）` : ''} ${detail.最近测试结果.时间 ?? ''}`
                  : '尚无测试结论'}
              </strong>
            </div>
          </div>
          <h4>状态历史（可追溯，离线不吞测试结果）</h4>
          <ul className="history-list">
            {(detail.history ?? []).map((item, index) => (
              <li key={index}>
                <span className="history-time">{item.时间 ?? '—'}</span>
                <span className={`history-result history-${item.结果 ?? ''}`}>{item.结果 ?? '—'}</span>
                <span>{item.动作 ?? '—'}{item.依据 ? `（依据：${item.依据}）` : ''}</span>
                <span className="history-detail">{item.说明 ?? ''}</span>
              </li>
            ))}
            {(detail.history ?? []).length === 0 && <li className="empty-state">暂无历史记录</li>}
          </ul>
        </aside>
      )}

      <footer className="page-foot">
        <span>共 {total} 条连接器管理记录</span>
        {noticeMessage && <span className="notice-text">{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
