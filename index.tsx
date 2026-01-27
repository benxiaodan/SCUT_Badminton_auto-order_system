import React, { useState, useEffect, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { Shield, Lock, User, Terminal, Eye, EyeOff, RefreshCw, Smartphone, Server, ToggleLeft, ToggleRight, AlertCircle, Calendar, Check, X, Zap, Crosshair, PlayCircle, StopCircle, Clock, Search, Timer, LogIn, Activity, Mail, ClipboardList, ShieldAlert, Trash2, Plus, MapPin } from 'lucide-react';

// 修改为相对路径，由 Nginx 统一转发，避免跨域和 IP 硬编码问题
const API_BASE_URL = '/api';

// --- Types ---
interface VenueSession {
    startTime: string;
    endTime: string;
    status: 'free' | 'sold' | 'reserved';
    price: number;
    venueId: string;
    stadiumId?: number;
    fixedPurpose?: string;
}

interface VenueRow {
    name: string;
    id: string;
    sessions: VenueSession[];
}

interface TaskInfo {
    id: string;
    type: 'snipe' | 'lock';
    status: string;
    info: string;
}

type VenueCache = Record<string, VenueRow[]>;

const PREDEFINED_VENUES = Array.from({ length: 16 }, (_, i) => `场地${i + 1}`);

const TIME_SLOTS = [
    "08:00-09:00", "09:00-10:00", "10:00-11:00", "11:00-12:00",
    "12:00-13:00", "13:00-14:00", "14:00-15:00", "15:00-16:00",
    "16:00-18:00", "18:00-20:00", "20:00-22:00"
];

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

// --- Helper Functions ---
const isTimeSlotPast = (selectedDateStr: string, timeSlot: string) => {
    const now = new Date();
    // 简单构建日期对象进行比较 (处理时区问题，只比较年月日)
    const todayStr = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');

    // 如果选择的日期在今天之前，肯定是过去
    if (selectedDateStr < todayStr) return true;
    // 如果选择的日期在今天之后，肯定不是过去
    if (selectedDateStr > todayStr) return false;

    // 如果是今天，比较小时
    const endHourStr = timeSlot.split('-')[1].split(':')[0];
    const endHour = parseInt(endHourStr, 10);
    const currentHour = now.getHours();
    return endHour <= currentHour;
};

const isTimeSlotFuture = (selectedDateStr: string, timeSlot: string) => {
    const now = new Date();
    const todayStr = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');

    // 1. 如果是未来日期，直接返回 true
    if (selectedDateStr > todayStr) return true;

    // 2. 如果是过去日期，直接返回 false
    if (selectedDateStr < todayStr) return false;

    // 3. 如果是今天，比较小时
    const startHourStr = timeSlot.split('-')[0].split(':')[0];
    const startHour = parseInt(startHourStr, 10);
    const currentHour = now.getHours();

    // 这里允许当前小时及以后（即还没有开始，或者刚开始但允许抢下一时段）
    return startHour >= currentHour;
};

// --- Independent Components ---

const LoadingOverlay = ({ message }: { message: string }) => (
    <div style={{
        position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
        background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(5px)',
        zIndex: 9999, display: 'flex', flexDirection: 'column',
        justifyContent: 'center', alignItems: 'center', color: '#fff'
    }}>
        <div className="spinner"></div>
        <div style={{ marginTop: 20, fontSize: 18, fontWeight: 'bold', textShadow: '0 2px 4px rgba(0,0,0,0.5)' }}>
            {message}
        </div>
        <style>{`
            .spinner {
                width: 50px; height: 50px;
                border: 5px solid rgba(255,255,255,0.3);
                border-radius: 50%;
                border-top-color: #fff;
                animation: spin 1s ease-in-out infinite;
            }
            @keyframes spin { to { transform: rotate(360deg); } }
        `}</style>
    </div>
);

const AccessDeniedModal = ({ isOpen, onClose }: any) => {
    if (!isOpen) return null;
    return (
        <div style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            background: 'rgba(0,0,0,0.7)', zIndex: 9999,
            display: 'flex', justifyContent: 'center', alignItems: 'center',
            backdropFilter: 'blur(5px)'
        }}>
            <div style={{
                background: '#fff', borderRadius: 16, padding: '30px', width: 400,
                boxShadow: '0 20px 60px rgba(0,0,0,0.4)', textAlign: 'center',
                animation: 'popIn 0.3s ease-out'
            }}>
                <div style={{ marginBottom: 20 }}>
                    <div style={{ background: '#fff1f0', display: 'inline-flex', padding: 15, borderRadius: '50%' }}>
                        <ShieldAlert size={48} color="#ff4d4f" />
                    </div>
                </div>
                <h2 style={{ margin: '0 0 10px 0', color: '#333' }}>访问受限</h2>
                <div style={{ fontSize: 16, color: '#666', lineHeight: 1.6, marginBottom: 25 }}>
                    需要获取权限请联系 <strong>ziqiangtang9@gmail.com</strong> 这个邮箱，并备注相关理由。
                </div>
                <button
                    onClick={onClose}
                    style={{
                        background: '#ff4d4f', color: '#fff', border: 'none', padding: '12px 30px',
                        borderRadius: 8, fontSize: 16, fontWeight: 'bold', cursor: 'pointer',
                        boxShadow: '0 4px 12px rgba(255, 77, 79, 0.3)'
                    }}
                >
                    关闭窗口
                </button>
            </div>
            <style>{`
                @keyframes popIn { from { transform: scale(0.9); opacity: 0; } to { transform: scale(1); opacity: 1; } }
            `}</style>
        </div>
    );
}

// 救援 2FA 弹窗组件
const Rescue2FAModal = ({ isOpen, code, setCode, onSubmit, onClose }: any) => {
    if (!isOpen) return null;
    return (
        <div style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            background: 'rgba(0,0,0,0.7)', zIndex: 9999,
            display: 'flex', justifyContent: 'center', alignItems: 'center',
            backdropFilter: 'blur(5px)'
        }}>
            <div style={{
                background: '#fff', borderRadius: 16, padding: '30px', width: 400,
                boxShadow: '0 20px 60px rgba(0,0,0,0.4)', textAlign: 'center',
                animation: 'popIn 0.3s ease-out'
            }}>
                <div style={{ marginBottom: 20 }}>
                    <div style={{ background: '#e6f7ff', display: 'inline-flex', padding: 15, borderRadius: '50%' }}>
                        <Smartphone size={48} color="#1890ff" />
                    </div>
                </div>
                <h2 style={{ margin: '0 0 10px 0', color: '#333' }}>会话已过期</h2>
                <div style={{ fontSize: 14, color: '#666', lineHeight: 1.6, marginBottom: 20 }}>
                    系统正在后台重新登录，检测到需要手机验证码。<br />
                    请输入您收到的验证码以完成登录：
                </div>
                <div style={{ display: 'flex', gap: 10, marginBottom: 20 }}>
                    <input
                        type="text"
                        value={code}
                        onChange={(e: any) => setCode(e.target.value)}
                        placeholder="请输入验证码"
                        style={{
                            flex: 1, padding: '12px 15px', fontSize: 16, border: '2px solid #1890ff',
                            borderRadius: 8, outline: 'none', textAlign: 'center', letterSpacing: 3
                        }}
                        onKeyDown={(e: any) => e.key === 'Enter' && onSubmit()}
                        autoFocus
                    />
                </div>
                <div style={{ display: 'flex', gap: 10 }}>
                    <button
                        onClick={onClose}
                        style={{
                            flex: 1, background: '#f5f5f5', color: '#666', border: 'none', padding: '12px',
                            borderRadius: 8, fontSize: 15, cursor: 'pointer'
                        }}
                    >
                        取消
                    </button>
                    <button
                        onClick={onSubmit}
                        style={{
                            flex: 2, background: '#1890ff', color: '#fff', border: 'none', padding: '12px',
                            borderRadius: 8, fontSize: 15, fontWeight: 'bold', cursor: 'pointer',
                            boxShadow: '0 4px 12px rgba(24, 144, 255, 0.3)'
                        }}
                    >
                        验证并刷新
                    </button>
                </div>
            </div>
            <style>{`
                @keyframes popIn { from { transform: scale(0.9); opacity: 0; } to { transform: scale(1); opacity: 1; } }
            `}</style>
        </div>
    );
}


const LogTerminal = ({ logs, style }: { logs: string[], style?: React.CSSProperties }) => {
    const scrollRef = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }, [logs]);

    return (
        <div style={{
            background: '#282c34', // 换成 VS Code 风格的深色背景
            color: '#abb2bf',
            borderRadius: 8,
            padding: 15,
            fontFamily: '"JetBrains Mono", Consolas, monospace',
            fontSize: 13,
            lineHeight: 1.6,
            overflowY: 'auto',
            border: '1px solid #3e4451',
            boxShadow: 'inset 0 2px 10px rgba(0,0,0,0.2)',
            ...style
        }} ref={scrollRef}>
            {logs.length === 0 && <div style={{ color: '#5c6370', fontStyle: 'italic' }}>等待系统日志输出...</div>}
            {logs.map((log, i) => {
                let color = '#abb2bf';
                // 优化日志配色
                if (log.includes('成功') || log.includes('Success') || log.includes('✅') || log.includes('🎉')) color = '#98c379'; // Green
                else if (log.includes('失败') || log.includes('Error') || log.includes('❌') || log.includes('⚠️')) color = '#e06c75'; // Red
                else if (log.includes('监控') || log.includes('Task') || log.includes('Lock')) color = '#61afef'; // Blue
                else if (log.includes('扫描') || log.includes('嗅探')) color = '#c678dd'; // Purple
                else if (log.includes('输入') || log.includes('点击')) color = '#e5c07b'; // Yellow
                else if (log.includes('邮件') || log.includes('Email')) color = '#56b6c2'; // Cyan

                return (
                    <div key={i} style={{ color, whiteSpace: 'pre-wrap', marginBottom: 4, display: 'flex' }}>
                        <span style={{ opacity: 0.5, marginRight: 10, minWidth: 60 }}>{log.split(']')[0] + ']'}</span>
                        <span>{log.split(']').slice(1).join(']')}</span>
                    </div>
                );
            })}
        </div>
    );
};

const BookingModal = ({ selectedCell, setSelectedCell, selectedDate, handleDirectBooking, handleLockBooking }: any) => {
    if (!selectedCell) return null;
    const { venue, time, session } = selectedCell;
    const canShowLock = isTimeSlotFuture(selectedDate, time);

    return (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.6)', display: 'flex', justifyContent: 'center', alignItems: 'center', zIndex: 100, backdropFilter: 'blur(3px)' }}>
            <div style={{ background: '#fff', padding: 'clamp(20px, 5vw, 30px)', borderRadius: 16, width: '90vw', maxWidth: 380, boxShadow: '0 20px 60px rgba(0,0,0,0.3)', transform: 'translateY(-20px)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 25, alignItems: 'center' }}>
                    <h3 style={{ margin: 0, fontSize: 20, display: 'flex', alignItems: 'center', gap: 8 }}><Activity size={20} color="#1890ff" /> 确认预定</h3>
                    <div onClick={() => setSelectedCell(null)} style={{ cursor: 'pointer', padding: 5, borderRadius: '50%', background: '#f5f5f5' }}><X size={18} /></div>
                </div>

                <div style={{ background: '#f8f9fa', padding: 20, borderRadius: 12, marginBottom: 25, border: '1px solid #eee' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                        <span style={{ color: '#666' }}>日期</span>
                        <strong style={{ fontSize: 15 }}>{selectedDate}</strong>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                        <span style={{ color: '#666' }}>时间</span>
                        <strong style={{ fontSize: 15 }}>{time}</strong>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                        <span style={{ color: '#666' }}>场地</span>
                        <strong style={{ fontSize: 15 }}>{venue.name}</strong>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', borderTop: '1px dashed #ddd', paddingTop: 10, marginTop: 10 }}>
                        <span style={{ color: '#666' }}>价格</span>
                        <strong style={{ color: '#ff4d4f', fontSize: 18 }}>￥{session.price}</strong>
                    </div>
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                    <button onClick={handleDirectBooking} style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, padding: '14px', background: 'linear-gradient(135deg, #1890ff 0%, #096dd9 100%)', color: '#fff', border: 'none', borderRadius: 8, cursor: 'pointer', fontSize: 16, fontWeight: 'bold', boxShadow: '0 4px 15px rgba(24, 144, 255, 0.3)' }}>
                        <Zap size={20} fill="#fff" /> 立即预定 (单次)
                    </button>

                    {canShowLock && (
                        <button onClick={handleLockBooking} style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, padding: '14px', background: 'linear-gradient(135deg, #722ed1 0%, #531dab 100%)', color: '#fff', border: 'none', borderRadius: 8, cursor: 'pointer', fontSize: 16, fontWeight: 'bold', boxShadow: '0 4px 15px rgba(114, 46, 209, 0.3)' }}>
                            <Lock size={20} /> 无限锁场 (自动续订)
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
};

const OrdersModal = ({ isOpen, onClose, token, username }: any) => {
    const [activeTab, setActiveTab] = useState<'unpaid' | 'paid' | 'refund' | 'closed'>('unpaid');
    const [orders, setOrders] = useState<any[]>([]);
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        if (isOpen && token) {
            fetchOrders();
        }
    }, [isOpen, token]); // 移除 activeTab 依赖，切换 Tab 不请求

    const fetchOrders = async () => {
        setLoading(true);
        try {
            const res = await fetch(`${API_BASE_URL}/orders`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token, type: 'all', username, refreshAll: true })
            });
            const data = await res.json();
            if (data.status === 'success' && data.data && data.data.records) {
                setOrders(data.data.records);
            } else {
                setOrders([]);
            }
        } catch (e) {
            console.error("Fetch orders failed", e);
        }
        setLoading(false);
    };

    if (!isOpen) return null;

    const tabs = [
        { key: 'unpaid', label: '待支付', color: '#fa8c16' },
        { key: 'paid', label: '已支付', color: '#52c41a' },
        { key: 'refund', label: '退款', color: '#722ed1' },
        { key: 'closed', label: '关闭', color: '#999' },
    ];

    return (
        <div style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            background: 'rgba(0,0,0,0.6)', zIndex: 110, backdropFilter: 'blur(3px)',
            display: 'flex', justifyContent: 'center', alignItems: 'center'
        }}>
            <div style={{
                background: '#fff', width: '95vw', maxWidth: 800, height: '80vh', maxHeight: 600, borderRadius: 16,
                boxShadow: '0 20px 60px rgba(0,0,0,0.3)', display: 'flex', flexDirection: 'column',
                overflow: 'hidden'
            }}>
                <div style={{ padding: '20px 25px', borderBottom: '1px solid #eee', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <h3 style={{ margin: 0, fontSize: 18, display: 'flex', alignItems: 'center', gap: 8 }}><ClipboardList size={20} color="#1890ff" /> 我的订单</h3>
                    <div onClick={onClose} style={{ cursor: 'pointer', padding: 6, borderRadius: '50%', background: '#f5f5f5' }}><X size={18} /></div>
                </div>

                <div style={{ display: 'flex', padding: '10px 25px', gap: 20, borderBottom: '1px solid #f0f0f0' }}>
                    {tabs.map(t => (
                        <div key={t.key}
                            onClick={() => setActiveTab(t.key as any)}
                            style={{
                                padding: '10px 5px', cursor: 'pointer', fontSize: 14, fontWeight: 'bold',
                                color: activeTab === t.key ? '#1890ff' : '#666',
                                borderBottom: activeTab === t.key ? '2px solid #1890ff' : '2px solid transparent',
                                transition: '0.2s'
                            }}>
                            {t.label}
                        </div>
                    ))}
                </div>

                <div style={{ flex: 1, overflow: 'auto', padding: 25, background: '#fafafa' }}>
                    {loading ? (
                        <div style={{ textAlign: 'center', padding: 40, color: '#999' }}>正在加载订单数据...</div>
                    ) : orders.length === 0 ? (
                        <div style={{ textAlign: 'center', padding: 40, color: '#ccc' }}>暂无相关订单</div>
                    ) : (
                        <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff', borderRadius: 8, overflow: 'hidden', boxShadow: '0 2px 8px rgba(0,0,0,0.05)' }}>
                            <thead style={{ background: '#f5f5f5' }}>
                                <tr>
                                    <th style={{ padding: 12, textAlign: 'left', fontSize: 13, color: '#666' }}>项目</th>
                                    <th style={{ padding: 12, textAlign: 'left', fontSize: 13, color: '#666' }}>场地</th>
                                    <th style={{ padding: 12, textAlign: 'left', fontSize: 13, color: '#666' }}>时间</th>
                                    <th style={{ padding: 12, textAlign: 'center', fontSize: 13, color: '#666' }}>场数</th>
                                    <th style={{ padding: 12, textAlign: 'right', fontSize: 13, color: '#666' }}>金额</th>
                                    <th style={{ padding: 12, textAlign: 'center', fontSize: 13, color: '#666' }}>状态</th>
                                </tr>
                            </thead>
                            <tbody>
                                {orders.filter((o: any) => o.statusType === activeTab).map((o: any, idx) => (
                                    <tr key={idx} style={{ borderBottom: '1px solid #f0f0f0' }}>
                                        <td style={{ padding: 12, fontSize: 14, fontWeight: 'bold' }}>{o.fieldName || '羽毛球'}</td>
                                        <td style={{ padding: 12, fontSize: 14, color: '#1890ff', fontWeight: 'bold' }}>{o.venueName}</td>
                                        <td style={{ padding: 12, fontSize: 13, color: '#333' }}>
                                            {o.belongDate} <br />
                                            <span style={{ color: '#999', fontSize: 12 }}>{o.startTime}-{o.endTime}</span>
                                        </td>
                                        <td style={{ padding: 12, textAlign: 'center', fontSize: 13 }}>1</td>
                                        <td style={{ padding: 12, textAlign: 'right', fontSize: 14, fontWeight: 'bold', color: '#ff4d4f' }}>￥{o.price}</td>
                                        <td style={{ padding: 12, textAlign: 'center' }}>
                                            <span style={{
                                                padding: '4px 10px', borderRadius: 4, fontSize: 12,
                                                background: activeTab === 'unpaid' ? '#fff7e6' : (activeTab === 'paid' ? '#f6ffed' : '#f5f5f5'),
                                                color: activeTab === 'unpaid' ? '#fa8c16' : (activeTab === 'paid' ? '#389e0d' : '#999'),
                                                border: `1px solid ${activeTab === 'unpaid' ? '#ffd591' : (activeTab === 'paid' ? '#b7eb8f' : '#d9d9d9')}`
                                            }}>
                                                {tabs.find(t => t.key === activeTab)?.label}
                                            </span>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    )}
                </div>
            </div>
        </div>
    );
};

const SniperPanel = ({
    dateOptions, sniperDate, setSniperDate,
    sniperTime, setSniperTime,
    sniperLockMode, setSniperLockMode,
    handleStartMonitor, logs
}: any) => {
    return (
        <div style={{ marginTop: 20, padding: 25, background: '#fff', borderRadius: 12, border: '1px solid #e8e8e8', boxShadow: '0 4px 12px rgba(0,0,0,0.02)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 20, borderBottom: '1px solid #f0f0f0', paddingBottom: 15 }}>
                <div style={{ background: '#fff7e6', padding: 8, borderRadius: 8 }}><Crosshair size={24} color="#fa8c16" /></div>
                <div>
                    <h3 style={{ margin: 0, fontSize: 18 }}>自动捡漏 & 锁场监控</h3>
                    <div style={{ fontSize: 12, color: '#999', marginTop: 4 }}>设置监控参数，系统将全自动运行</div>
                </div>
            </div>

            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 20, alignItems: 'end', marginBottom: 25 }}>
                <div style={{ flex: 1, minWidth: 200 }}>
                    <div style={{ fontSize: 13, fontWeight: 'bold', color: '#444', marginBottom: 8 }}>目标日期</div>
                    <select value={sniperDate} onChange={e => setSniperDate(e.target.value)} style={{ width: '100%', padding: '10px 15px', borderRadius: 8, border: '1px solid #d9d9d9', background: '#fafafa', fontSize: 14 }}>
                        {dateOptions.map((opt: any) => <option key={opt.date} value={opt.date}>{opt.label}</option>)}
                    </select>
                </div>
                <div style={{ flex: 1, minWidth: 200 }}>
                    <div style={{ fontSize: 13, fontWeight: 'bold', color: '#444', marginBottom: 8 }}>时间段</div>
                    <select value={sniperTime} onChange={e => setSniperTime(e.target.value)} style={{ width: '100%', padding: '10px 15px', borderRadius: 8, border: '1px solid #d9d9d9', background: '#fafafa', fontSize: 14 }}>
                        {TIME_SLOTS.map(t => <option key={t} value={t}>{t}</option>)}
                    </select>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', height: 45 }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', userSelect: 'none', background: '#f9f9f9', padding: '10px 15px', borderRadius: 8, border: '1px solid #eee' }}>
                        <input type="checkbox" checked={sniperLockMode} onChange={e => setSniperLockMode(e.target.checked)} style={{ width: 20, height: 20, accentColor: '#722ed1' }} />
                        <span style={{ fontSize: 14, fontWeight: '500' }}>开启无限锁场模式</span>
                    </label>
                </div>
                <button onClick={handleStartMonitor} style={{ flex: 1, minWidth: 200, height: 45, background: sniperLockMode ? 'linear-gradient(135deg, #722ed1 0%, #531dab 100%)' : 'linear-gradient(135deg, #fa8c16 0%, #d46b08 100%)', color: '#fff', border: 'none', borderRadius: 8, cursor: 'pointer', fontWeight: 'bold', fontSize: 16, boxShadow: '0 4px 12px rgba(0,0,0,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
                    {sniperLockMode ? <Lock size={18} /> : <Zap size={18} />}
                    {sniperLockMode ? '启动锁场监控' : '启动自动订场（锁场）'}
                </button>
            </div>

            <div style={{ background: '#f6ffed', border: '1px solid #b7eb8f', padding: '12px 15px', borderRadius: 8, fontSize: 13, color: '#389e0d', marginBottom: 20, display: 'flex', alignItems: 'start', gap: 8 }}>
                <AlertCircle size={16} style={{ marginTop: 2, flexShrink: 0 }} />
                <div>
                    <strong>功能说明：</strong><br />
                    1. <strong>自动捡漏</strong>：在设置时间后监控场地数据，一旦发现可预订场地，系统直接提交预定订单。<br />
                    2. <strong>锁场功能</strong>：主要用于保护场地。在订单付款时间即将截止时，系统会自动重新提交订单，从而持续锁定场地，直到您手动停止。
                </div>
            </div>

            <div style={{ marginTop: 15 }}>
                <div style={{ fontSize: 13, fontWeight: 'bold', color: '#444', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                    <Terminal size={16} color="#666" /> 系统运行日志
                </div>
                <LogTerminal logs={logs} style={{ height: 220 }} />
            </div>
        </div>
    );
};

const TaskMonitor = ({ tasks, fetchTasks, stopTask }: any) => {
    // 只显示活跃任务（过滤掉已停止的任务）
    const taskList = Object.entries(tasks).filter(([id, t]: any) => t.status !== 'Stopped');
    if (taskList.length === 0) return null;
    return (
        <div style={{ position: 'fixed', bottom: 15, right: 15, width: 'min(340px, calc(100vw - 30px))', background: '#fff', boxShadow: '0 8px 30px rgba(0,0,0,0.15)', borderRadius: 12, border: '1px solid #eee', overflow: 'hidden', zIndex: 90 }}>
            <div style={{ padding: '12px 15px', background: 'linear-gradient(to right, #fafafa, #f5f5f5)', borderBottom: '1px solid #eee', fontWeight: 'bold', fontSize: 14, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}><Activity size={16} color="#1890ff" /> 活跃任务 ({taskList.length})</span>
                <span style={{ fontSize: 12, color: '#1890ff', cursor: 'pointer', background: '#e6f7ff', padding: '2px 8px', borderRadius: 4 }} onClick={fetchTasks}>刷新</span>
            </div>
            <div style={{ maxHeight: 200, overflowY: 'auto' }}>
                {taskList.map(([id, t]: any) => (
                    <div key={id} style={{ padding: '12px 15px', borderBottom: '1px solid #f0f0f0', display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 13 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                            <div style={{ padding: 6, borderRadius: 6, background: t.type === 'snipe' ? '#fff7e6' : '#f9f0ff' }}>
                                {t.type === 'snipe' ? <Crosshair size={16} color="#fa8c16" /> : <Lock size={16} color="#722ed1" />}
                            </div>
                            <div>
                                <div style={{ fontWeight: 'bold', color: '#333' }}>{t.type === 'snipe' ? '自动订场' : '无限锁场'}</div>
                                <div style={{ color: '#999', fontSize: 11, marginTop: 2 }}>{t.info}</div>
                            </div>
                        </div>
                        {t.status !== 'Stopped' && (
                            <button onClick={() => stopTask(id)} style={{ border: '1px solid #ffccc7', background: '#fff1f0', color: '#ff4d4f', padding: '4px 10px', borderRadius: 6, cursor: 'pointer', fontSize: 11, fontWeight: 'bold' }}>停止</button>
                        )}
                    </div>
                ))}
            </div>
        </div>
    );
};

const LoginView = ({
    username, setUsername, password, setPassword, email, setEmail, handleLogin, status, errorMsg, verify2FA, codeValue, setCodeValue, logs
}: any) => {
    const [loginMsg, setLoginMsg] = useState("正在连接服务器...");

    // 登录状态文字轮播
    useEffect(() => {
        if (status === 'checking') {
            const msgs = ["正在连接服务器...", "正在校验账号...", "等待SSO跳转...", "获取Token中..."];
            let i = 0;
            const timer = setInterval(() => {
                setLoginMsg(msgs[i % msgs.length]);
                i++;
            }, 800);
            return () => clearInterval(timer);
        }
    }, [status]);

    const handleKeyDown = (e: React.KeyboardEvent, target: 'un' | 'pd' | 'code' | 'email') => {
        if (e.key === 'Enter') {
            if (target === 'un') document.getElementById('password-input')?.focus();
            else if (target === 'pd') document.getElementById('email-input')?.focus();
            else if (target === 'email') handleLogin();
            else if (target === 'code') verify2FA();
        }
    };

    return (
        <div style={{
            width: '100vw', height: '100vh',
            display: 'flex', justifyContent: 'center', alignItems: 'center',
            backgroundImage: 'url("./background.jpg")',
            backgroundSize: 'cover', backgroundPosition: 'center'
        }}>
            <div style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.4)', backdropFilter: 'blur(3px)' }}></div>

            <div style={{
                position: 'relative',
                width: '90vw',
                maxWidth: 500,
                background: 'rgba(255, 255, 255, 0.85)',
                backdropFilter: 'blur(15px)',
                borderRadius: 24,
                boxShadow: '0 20px 80px rgba(0,0,0,0.4)',
                padding: 'clamp(25px, 6vw, 50px)',
                display: 'flex', flexDirection: 'column', gap: 20,
                border: '1px solid rgba(255,255,255,0.5)'
            }}>
                <div style={{ textAlign: 'center', marginBottom: 10 }}>
                    <div style={{ display: 'inline-flex', padding: 15, background: '#1890ff', borderRadius: '50%', marginBottom: 15, boxShadow: '0 10px 20px rgba(24,144,255,0.3)' }}>
                        <div style={{ color: '#fff', fontWeight: 'bold', fontSize: 24 }}>🏸</div>
                    </div>
                    <h1 style={{ margin: 0, fontSize: 28, color: '#333', fontWeight: '800' }}>华工羽毛球订场助手</h1>
                    <p style={{ margin: '10px 0 0 0', color: '#666' }}>BY BENXIAODAN</p>
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: 15 }}>
                    <div style={{ position: 'relative' }}>
                        <User size={20} color="#999" style={{ position: 'absolute', left: 15, top: 15 }} />
                        <input
                            id="username-input"
                            placeholder="统一认证账号"
                            style={{ width: '100%', padding: '15px 15px 15px 45px', border: '1px solid #ddd', borderRadius: 12, background: '#fff', fontSize: 16, outline: 'none', transition: '0.2s' }}
                            value={username}
                            onChange={e => setUsername(e.target.value)}
                            onKeyDown={(e) => handleKeyDown(e, 'un')}
                            autoFocus
                        />
                    </div>
                    <div style={{ position: 'relative' }}>
                        <Lock size={20} color="#999" style={{ position: 'absolute', left: 15, top: 15 }} />
                        <input
                            id="password-input"
                            type="password"
                            placeholder="统一认证密码"
                            style={{ width: '100%', padding: '15px 15px 15px 45px', border: '1px solid #ddd', borderRadius: 12, background: '#fff', fontSize: 16, outline: 'none' }}
                            value={password}
                            onChange={e => setPassword(e.target.value)}
                            onKeyDown={(e) => handleKeyDown(e, 'pd')}
                        />
                    </div>
                    <div style={{ position: 'relative' }}>
                        <Mail size={20} color="#999" style={{ position: 'absolute', left: 15, top: 15 }} />
                        <input
                            id="email-input"
                            placeholder="接收通知邮箱"
                            style={{ width: '100%', padding: '15px 15px 15px 45px', border: '1px solid #ddd', borderRadius: 12, background: '#fff', fontSize: 16, outline: 'none' }}
                            value={email}
                            onChange={e => setEmail(e.target.value)}
                            onKeyDown={(e) => handleKeyDown(e, 'email')}
                        />
                    </div>
                </div>

                {status === '2fa_needed' && (
                    <div style={{ background: '#f6ffed', padding: 20, borderRadius: 12, border: '1px solid #b7eb8f', animation: 'fadeIn 0.5s' }}>
                        <div style={{ marginBottom: 10, fontSize: 14, color: '#389e0d', display: 'flex', alignItems: 'center', gap: 5 }}>
                            <Smartphone size={16} /> 请输入手机验证码 (2FA)
                        </div>
                        <div style={{ display: 'flex', gap: 10 }}>
                            <input
                                placeholder="6位验证码"
                                style={{ flex: 1, padding: 12, border: '1px solid #ddd', borderRadius: 8, fontSize: 16, textAlign: 'center', letterSpacing: 2 }}
                                value={codeValue}
                                onChange={e => setCodeValue(e.target.value)}
                                onKeyDown={(e) => handleKeyDown(e, 'code')}
                            />
                            <button onClick={verify2FA} style={{ background: '#389e0d', color: '#fff', border: 'none', padding: '0 25px', borderRadius: 8, cursor: 'pointer', fontWeight: 'bold', fontSize: 15 }}>验证</button>
                        </div>
                    </div>
                )}

                {status === 'error' && <div style={{ color: '#ff4d4f', fontSize: 14, textAlign: 'center', background: '#fff1f0', padding: 10, borderRadius: 8, border: '1px solid #ffccc7' }}><AlertCircle size={14} style={{ verticalAlign: 'middle', marginRight: 5 }} />{errorMsg}</div>}

                {(status === 'idle' || status === 'error') && (
                    <button onClick={handleLogin} style={{ width: '100%', padding: 16, background: 'linear-gradient(135deg, #1890ff 0%, #096dd9 100%)', color: '#fff', border: 'none', borderRadius: 12, fontSize: 18, cursor: 'pointer', fontWeight: 'bold', boxShadow: '0 8px 20px rgba(24,144,255,0.4)', transition: 'transform 0.1s' }}>
                        <LogIn size={20} style={{ verticalAlign: 'middle', marginRight: 8 }} /> 登录系统
                    </button>
                )}

                {status === 'checking' && (
                    <button disabled style={{ width: '100%', padding: 16, background: '#f0f0f0', color: '#999', border: 'none', borderRadius: 12, fontSize: 16, cursor: 'not-allowed', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10 }}>
                        <div className="mini-spinner"></div> {loginMsg}
                    </button>
                )}

                <div style={{ marginTop: 10, borderTop: '1px solid #eee', paddingTop: 15 }}>
                    <div style={{ fontSize: 13, fontWeight: 'bold', color: '#666', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 5 }}>
                        <Terminal size={14} /> 实时日志
                    </div>
                    <LogTerminal logs={logs} style={{ height: 100, fontSize: 11 }} />
                </div>
            </div>

            <style>{`
                .mini-spinner { width: 16px; height: 16px; border: 2px solid #ccc; border-top-color: #666; border-radius: 50%; animation: spin 1s infinite linear; }
                @keyframes fadeIn { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }
            `}</style>
        </div>
    );
};

const DashboardView = ({
    autoRefresh, setAutoRefresh, fetchAllWeekData, token, setView,
    dateOptions, selectedDate, setSelectedDate,
    status, allVenueData, setSelectedCell,
    sniperDate, setSniperDate, sniperTime, setSniperTime, sniperLockMode, setSniperLockMode, handleStartMonitor, logs,
    tasks, fetchTasks, stopTask,
    selectedCell, handleDirectBooking, handleLockBooking,
    username, handleLogout // New prop
}: any) => {

    const [showOrders, setShowOrders] = useState(false);

    const getSession = (venueName: string, timeSlot: string) => {
        const currentDayData = allVenueData[selectedDate] || [];
        const venue = currentDayData.find((v: any) => v.name === venueName);
        if (!venue) return { session: null, venue: null };
        const start = timeSlot.split('-')[0];
        return { session: venue.sessions.find((s: any) => s.startTime === start), venue };
    };

    return (
        <div style={{ width: '98vw', height: '95vh', background: '#fff', borderRadius: 20, display: 'flex', flexDirection: 'column', overflow: 'hidden', boxShadow: '0 10px 40px rgba(0,0,0,0.1)' }}>
            {/* Header */}
            <div className="header-container" style={{ padding: 'clamp(10px, 2vw, 20px) clamp(15px, 3vw, 30px)', background: '#fff', borderBottom: '1px solid #eee', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
                <h2 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 8, color: '#1f1f1f', fontSize: 'clamp(14px, 3.5vw, 24px)', whiteSpace: 'nowrap' }}>
                    <span style={{ fontSize: 'clamp(16px, 4vw, 28px)' }}>🏸</span> <span className="hide-on-mobile">华工西体羽毛球场地</span><span className="show-on-mobile-only" style={{ display: 'none' }}></span>预定表
                </h2>
                <div className="btn-group" style={{ display: 'flex', gap: 'clamp(4px, 1vw, 10px)', alignItems: 'center', flexWrap: 'wrap' }}>
                    <div className="header-btn" style={{ padding: '0 clamp(8px, 1.5vw, 15px)', display: 'flex', alignItems: 'center', gap: 4, color: '#666', fontSize: 'clamp(11px, 2vw, 14px)', background: '#f5f5f5', borderRadius: 6, height: 'clamp(28px, 5vw, 40px)' }}>
                        <User size={14} /> <span className="hide-on-mobile">账号:</span> <strong>{username}</strong>
                    </div>

                    <button className="header-btn" onClick={() => setView('monthly')} style={{ padding: 'clamp(6px, 1.2vw, 10px) clamp(10px, 2vw, 20px)', background: '#f9f0ff', color: '#722ed1', border: '1px solid #d3adf7', borderRadius: 6, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4, fontSize: 'clamp(11px, 2vw, 14px)', fontWeight: 'bold' }}>
                        <Calendar size={14} /> <span className="hide-on-mobile">月场</span>预定
                    </button>

                    <button className="header-btn" onClick={() => setShowOrders(true)} style={{ padding: 'clamp(6px, 1.2vw, 10px) clamp(10px, 2vw, 20px)', background: '#fff7e6', color: '#fa8c16', border: '1px solid #ffd591', borderRadius: 6, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4, fontSize: 'clamp(11px, 2vw, 14px)', fontWeight: 'bold' }}>
                        <ClipboardList size={14} /> <span className="hide-on-mobile">我的</span>订单
                    </button>

                    <button
                        className="header-btn"
                        onClick={() => setAutoRefresh(!autoRefresh)}
                        style={{ padding: 'clamp(6px, 1.2vw, 10px) clamp(10px, 2vw, 20px)', background: autoRefresh ? '#f6ffed' : '#fff', color: autoRefresh ? '#389e0d' : '#555', border: autoRefresh ? '1px solid #b7eb8f' : '1px solid #ddd', borderRadius: 6, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4, fontSize: 'clamp(11px, 2vw, 14px)', fontWeight: '500', transition: '0.2s' }}>
                        <Timer size={14} /> {autoRefresh ? <><span className="hide-on-mobile">自动刷新:</span> 开</> : <><span className="hide-on-mobile">自动刷新:</span> 关</>}
                    </button>

                    <button className="header-btn" onClick={() => fetchAllWeekData(token!)} style={{ padding: 'clamp(6px, 1.2vw, 10px) clamp(10px, 2vw, 20px)', background: '#1890ff', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4, fontSize: 'clamp(11px, 2vw, 14px)', fontWeight: 'bold', boxShadow: '0 2px 8px rgba(24,144,255,0.3)' }}>
                        <RefreshCw size={14} /> 刷新<span className="hide-on-mobile">全周数据</span>
                    </button>

                    {/* 修复：使用 handleLogout 正确重置状态 */}
                    <button className="header-btn" onClick={handleLogout} style={{ padding: 'clamp(6px, 1.2vw, 10px) clamp(10px, 2vw, 20px)', background: '#fff', color: '#666', border: '1px solid #ddd', borderRadius: 6, cursor: 'pointer', fontSize: 'clamp(11px, 2vw, 14px)', display: 'flex', alignItems: 'center', gap: 4, fontWeight: '500' }}>
                        <LogIn size={14} /> 退出
                    </button>
                </div>
            </div>

            {/* Date Tabs */}
            <div className="date-tabs" style={{ display: 'flex', gap: 'clamp(4px, 1vw, 12px)', padding: 'clamp(8px, 1.5vw, 15px) clamp(10px, 2vw, 30px)', background: '#f7f9fc', borderBottom: '1px solid #eee', overflowX: 'auto' }}>
                {dateOptions.map((opt: any) => (
                    <button key={opt.date} onClick={() => setSelectedDate(opt.date)} style={{ padding: 'clamp(6px, 1vw, 10px) clamp(10px, 1.8vw, 20px)', borderRadius: 8, border: selectedDate === opt.date ? 'none' : '1px solid #e0e0e0', background: selectedDate === opt.date ? '#1890ff' : '#fff', color: selectedDate === opt.date ? '#fff' : '#666', cursor: 'pointer', fontWeight: selectedDate === opt.date ? 'bold' : 'normal', fontSize: 'clamp(11px, 2vw, 14px)', boxShadow: selectedDate === opt.date ? '0 2px 8px rgba(24,144,255,0.3)' : 'none', transition: '0.2s', whiteSpace: 'nowrap', flexShrink: 0 }}>
                        {opt.label}
                    </button>
                ))}
            </div>

            {/* Matrix Table */}
            <div style={{ flex: 1, overflow: 'auto', padding: 20, background: '#fff' }}>
                {status === 'fetching_data' ? (
                    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', color: '#999' }}>
                        <div className="spin" style={{ width: 40, height: 40, border: '4px solid #f3f3f3', borderTop: '4px solid #1890ff', borderRadius: '50%' }}></div>
                        <div style={{ marginTop: 15, fontSize: 16 }}>正在同步场地数据...</div>
                    </div>
                ) : (
                    <>
                        <table style={{ width: '100%', borderCollapse: 'separate', borderSpacing: '6px', fontSize: 13 }}>
                            <thead>
                                <tr>
                                    <th style={{ padding: 'clamp(6px, 1.5vw, 15px)', background: '#fafafa', minWidth: 'clamp(50px, 10vw, 100px)', position: 'sticky', top: 0, zIndex: 10, borderBottom: '1px solid #eee', fontWeight: 'bold', color: '#333', fontSize: 'clamp(10px, 2vw, 13px)' }}>时间段</th>
                                    {PREDEFINED_VENUES.map(v => (<th key={v} style={{ padding: 'clamp(6px, 1.5vw, 15px)', background: '#fafafa', minWidth: 'clamp(45px, 9vw, 90px)', position: 'sticky', top: 0, zIndex: 10, borderBottom: '1px solid #eee', fontWeight: 'bold', color: '#333', fontSize: 'clamp(10px, 2vw, 13px)' }}>{v}</th>))}
                                </tr>
                            </thead>
                            <tbody>
                                {TIME_SLOTS.map((timeSlot, idx) => {
                                    const isPast = isTimeSlotPast(selectedDate, timeSlot);
                                    return (
                                        <tr key={timeSlot}>
                                            <td style={{ padding: 'clamp(4px, 1vw, 12px)', background: '#fff', fontWeight: 'bold', color: '#666', textAlign: 'center', borderRadius: 6, boxShadow: 'inset 0 0 0 1px #eee', fontSize: 'clamp(9px, 1.8vw, 12px)' }}>{timeSlot}</td>
                                            {PREDEFINED_VENUES.map(venueName => {
                                                const { session, venue } = getSession(venueName, timeSlot);

                                                // Default: Empty/Null slot
                                                let style: any = {
                                                    background: '#fafafa', color: '#ccc', textAlign: 'center',
                                                    padding: 'clamp(2px, 0.5vw, 4px)', borderRadius: 6, cursor: 'default', height: 'clamp(28px, 6vw, 60px)',
                                                    boxShadow: 'inset 0 0 0 1px #f0f0f0', transition: 'all 0.2s', fontSize: 'clamp(9px, 1.8vw, 12px)'
                                                };
                                                let content: React.ReactNode = '-';
                                                let onClick = undefined;

                                                if (isPast) {
                                                    style.background = '#f9f9f9';
                                                    style.color = '#ccc';
                                                    style.cursor = 'not-allowed';
                                                    content = '已过期';
                                                    if (session && session.status === 'sold') content = '已售';
                                                } else if (session) {
                                                    if (session.status === 'free') {
                                                        style.cursor = 'pointer';

                                                        if (session.price > 0) {
                                                            // Paid: Brand color
                                                            style.background = '#e6f7ff'; // Light blue
                                                            style.color = '#1890ff';
                                                            style.border = '1px solid #91d5ff';
                                                            style.boxShadow = '0 2px 5px rgba(24,144,255,0.1)';
                                                            content = <div style={{ fontWeight: 'bold', fontSize: 'clamp(10px, 2vw, 14px)' }}>￥{session.price}</div>;
                                                        } else {
                                                            // Free: Mint Green
                                                            style.background = '#f6ffed';
                                                            style.color = '#389e0d';
                                                            style.border = '1px solid #b7eb8f';
                                                            style.boxShadow = '0 2px 5px rgba(56,158,13,0.1)';
                                                            content = <div style={{ fontWeight: 'bold', fontSize: 'clamp(10px, 2vw, 14px)' }}>免费</div>;
                                                        }
                                                        onClick = () => setSelectedCell({ venue, time: timeSlot, session });
                                                    } else if (session.status === 'sold') {
                                                        // Sold: Light Gray, disabled look
                                                        style.background = '#f5f5f5';
                                                        style.color = '#999';
                                                        style.border = '1px solid #eee';
                                                        style.cursor = 'not-allowed';
                                                        content = '已售';
                                                    } else {
                                                        // Reserved: Stripes
                                                        style.background = 'repeating-linear-gradient(45deg, #f5f5f5, #f5f5f5 10px, #e8e8e8 10px, #e8e8e8 20px)';
                                                        style.color = '#999';
                                                        style.border = '1px solid #ddd';
                                                        style.cursor = 'not-allowed';
                                                        style.fontSize = 'clamp(8px, 1.5vw, 11px)';
                                                        content = session.fixedPurpose || '预留';
                                                    }
                                                }

                                                return (
                                                    <td key={venueName} onClick={onClick} style={style}
                                                        onMouseEnter={(e) => {
                                                            if (!isPast && session?.status === 'free') {
                                                                e.currentTarget.style.transform = 'scale(1.05)';
                                                                e.currentTarget.style.zIndex = '2';
                                                            }
                                                        }}
                                                        onMouseLeave={(e) => {
                                                            e.currentTarget.style.transform = 'scale(1)';
                                                            e.currentTarget.style.zIndex = '1';
                                                        }}
                                                    >
                                                        {content}
                                                    </td>
                                                );
                                            })}
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>

                        <SniperPanel
                            dateOptions={dateOptions}
                            sniperDate={sniperDate} setSniperDate={setSniperDate}
                            sniperTime={sniperTime} setSniperTime={setSniperTime}
                            sniperLockMode={sniperLockMode} setSniperLockMode={setSniperLockMode}
                            handleStartMonitor={handleStartMonitor}
                            logs={logs}
                        />
                    </>
                )}
            </div>

            <BookingModal
                selectedCell={selectedCell} setSelectedCell={setSelectedCell}
                selectedDate={selectedDate}
                handleDirectBooking={handleDirectBooking}
                handleLockBooking={handleLockBooking}
            />
            <OrdersModal
                isOpen={showOrders} onClose={() => setShowOrders(false)}
                token={token} username={username}
            />
            <TaskMonitor tasks={tasks} fetchTasks={fetchTasks} stopTask={stopTask} />

            <style>{`
            .spin { animation: spin 1s linear infinite; }
            @keyframes spin { 100% { transform: rotate(360deg); } }
            
            /* 移动端响应式样式 */
            @media (max-width: 768px) {
                .hide-on-mobile { display: none !important; }
            }
          `}</style>
        </div>
    );
};

// ==========================================
// MonthlyBookingView 组件
// ==========================================

const MonthlyBookingView = ({
    username, token, setView, tasks, fetchTasks
}: {
    username: string, token: string, setView: (v: any) => void, tasks: any[], fetchTasks: () => void
}) => {
    // 状态管理
    const [targetYear, setTargetYear] = useState(new Date().getFullYear());
    const [targetMonth, setTargetMonth] = useState(new Date().getMonth() + 2 > 12 ? 1 : new Date().getMonth() + 2); // 默认下个月
    const [weekdays, setWeekdays] = useState<number[]>([]);
    // 改为多选时间段
    const [selectedTimeSlots, setSelectedTimeSlots] = useState<string[]>([]);
    const [selectedVenues, setSelectedVenues] = useState<string[]>([]);
    const [email, setEmail] = useState("");
    const [loading, setLoading] = useState(false);

    // 预设数据 - 可用时段列表
    const timeSlots = [
        "08:00-09:00", "09:00-10:00", "10:00-11:00", "11:00-12:00",
        "12:00-13:00", "13:00-14:00", "14:00-15:00", "15:00-16:00",
        "16:00-18:00", "18:00-20:00", "20:00-22:00"
    ];

    // 场地列表 (1-16号)
    const venueList = Array.from({ length: 16 }, (_, i) => ({
        id: (i + 1).toString(),
        name: `场地${i + 1}`
    }));

    // 复用之前的邮箱
    useEffect(() => {
        // 尝试从 localStorage 或 prop 获取邮箱
    }, []);

    // 独立管理月场任务列表
    const [monthlyTasks, setMonthlyTasks] = useState<any[]>([]);

    const fetchMonthlyTasks = async () => {
        try {
            const res = await fetch(`/api/monthly/tasks?username=${username}`);
            const data = await res.json();
            if (data.status === 'success') {
                // 按创建时间倒序
                const sorted = Object.values(data.tasks || {}).sort((a: any, b: any) =>
                    new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
                );
                setMonthlyTasks(sorted);
            }
        } catch (e) {
            console.error("Fetch monthly tasks failed", e);
        }
    };

    useEffect(() => {
        fetchMonthlyTasks();
    }, []);

    // 提交任务 - 支持批量创建
    const handleSubmit = async () => {
        if (weekdays.length === 0 || selectedTimeSlots.length === 0 || selectedVenues.length === 0) {
            alert("请至少选择一个工作日、一个时间段和一个场地！");
            return;
        }

        setLoading(true);
        const results: string[] = [];
        let successCount = 0;

        // 排序规则：星期 -> 场地(ID) -> 时间段
        // 注意：目前后端接口接受 venue_ids 列表，是在同一个任务中并发抢这几个场地。
        // 如果严格按照"场地排序"意味着要拆分场地为独立任务，但这会显著增加任务量。
        // 按照目前后端逻辑维持"并发抢多场"的优势，我们在这里对 venue_ids 进行排序后发送，
        // 确保后端处理时的一致性。
        const sortedWeekdays = [...weekdays].sort((a, b) => a - b);
        const sortedVenues = [...selectedVenues].sort((a, b) => Number(a) - Number(b));
        const sortedSlots = [...selectedTimeSlots].sort((a, b) => a.localeCompare(b));

        try {
            // 按照优先级顺序创建任务: Week > Venue(Internal Sort) > Time
            // 实际上对于创建任务的请求顺序：
            // 外层循环 Week
            // 内层循环 Time (因为 Time 必须拆分)
            // Venue 列表作为一个参数传递 (Backend handles concurrency)

            for (const day of sortedWeekdays) {
                // 每个时间段必须拆分为独立任务 (因为后端接口不支持列表)
                for (const slot of sortedSlots) {
                    const [start, end] = slot.split("-");

                    try {
                        const res = await fetch('/api/monthly/create', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                token,
                                username,
                                email,
                                target_year: targetYear,
                                target_month: targetMonth,
                                weekday: day,
                                start_time: start,
                                end_time: end,
                                venue_ids: sortedVenues // 传递已排序的场地列表
                            })
                        });

                        const data = await res.json();

                        if (data.status === 'success') {
                            successCount++;
                            // results.push(`✅ 周${day} ${slot}: 成功`);
                        } else {
                            // 优先显示 data.msg，如果没有则查找 detail (FastAPI 默认错误字段)
                            const errorMsg = data.msg || data.detail || JSON.stringify(data);
                            results.push(`❌ 周${day} ${slot}: ${errorMsg}`);
                        }
                    } catch (netErr) {
                        results.push(`❌ 周${day} ${slot}: 网络或解析错误 (${netErr})`);
                    }
                }
            }

            // 汇总结果
            if (results.length === 0 && successCount > 0) {
                alert(`全部任务创建成功！(共 ${successCount} 个)`);
            } else {
                const summary = `成功: ${successCount} 个\n失败: ${results.length} 个\n\n失败详情:\n${results.join('\n')}`;
                alert(summary);
            }

            fetchMonthlyTasks(); // 刷新列表
            // 不自动清空表单，方便用户微调后再次提交
        } catch (e) {
            alert("创建流程异常: " + e);
        } finally {
            setLoading(false);
        }
    };

    // 取消任务
    const handleCancel = async (taskId: string) => {
        if (!confirm("确定要取消这个月场预定任务吗？")) return;
        try {
            const res = await fetch('/api/monthly/cancel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_id: taskId, username })
            });
            const data = await res.json();
            if (data.status === 'success') {
                fetchMonthlyTasks();
            } else {
                alert(data.msg || data.detail || "取消失败");
            }
        } catch (e) {
            alert("取消失败: " + e);
        }
    };

    return (
        <div style={{ padding: 20, maxWidth: 800, margin: '0 auto' }}>
            {/* Header */}
            <div style={{ display: 'flex', alignItems: 'center', marginBottom: 30, gap: 15 }}>
                <button onClick={() => setView('dashboard')} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 5, fontSize: 16 }}>
                    ⬅ 返回仪表盘
                </button>
                <h1 style={{ margin: 0, fontSize: 24 }}>📅 月场自动抢票</h1>
            </div>

            {/* 配置卡片 */}
            <div style={{ background: '#fff', borderRadius: 16, padding: 25, boxShadow: '0 4px 20px rgba(0,0,0,0.05)', marginBottom: 30 }}>
                <h3 style={{ marginTop: 0, marginBottom: 20, display: 'flex', alignItems: 'center', gap: 8 }}>
                    <Plus size={20} color="#1890ff" /> 新建预约任务
                </h3>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
                    {/* 年月选择 */}
                    <div>
                        <label style={{ display: 'block', marginBottom: 8, fontWeight: 'bold', fontSize: 13, color: '#666' }}>目标月份</label>
                        <div style={{ display: 'flex', gap: 10 }}>
                            <select value={targetYear} onChange={e => setTargetYear(Number(e.target.value))} style={{ padding: 10, borderRadius: 8, border: '1px solid #ddd', flex: 1 }}>
                                <option value={2025}>2025年</option>
                                <option value={2026}>2026年</option>
                            </select>
                            <select value={targetMonth} onChange={e => setTargetMonth(Number(e.target.value))} style={{ padding: 10, borderRadius: 8, border: '1px solid #ddd', flex: 1 }}>
                                {Array.from({ length: 12 }, (_, i) => i + 1).map(m => (
                                    <option key={m} value={m}>{m}月</option>
                                ))}
                            </select>
                        </div>
                    </div>

                    {/* 周几选择 */}
                    <div>
                        <label style={{ display: 'block', marginBottom: 8, fontWeight: 'bold', fontSize: 13, color: '#666' }}>周几（可多选）</label>
                        <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
                            {[1, 2, 3, 4, 5, 6, 7].map(day => (
                                <button
                                    key={day}
                                    onClick={() => {
                                        if (weekdays.includes(day)) setWeekdays(weekdays.filter(d => d !== day));
                                        else setWeekdays([...weekdays, day]);
                                    }}
                                    style={{
                                        padding: '8px 12px', borderRadius: 6, border: '1px solid #eee', cursor: 'pointer',
                                        background: weekdays.includes(day) ? '#1890ff' : '#f5f5f5',
                                        color: weekdays.includes(day) ? '#fff' : '#666'
                                    }}
                                >
                                    周{['一', '二', '三', '四', '五', '六', '日'][day - 1]}
                                </button>
                            ))}
                        </div>
                    </div>

                    {/* 时间段选择 - 多选 */}
                    <div style={{ gridColumn: '1 / -1' }}>
                        <label style={{ display: 'block', marginBottom: 8, fontWeight: 'bold', fontSize: 13, color: '#666' }}>时间段（可多选）</label>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
                            {timeSlots.map(slot => (
                                <button
                                    key={slot}
                                    onClick={() => {
                                        if (selectedTimeSlots.includes(slot)) {
                                            setSelectedTimeSlots(selectedTimeSlots.filter(s => s !== slot));
                                        } else {
                                            setSelectedTimeSlots([...selectedTimeSlots, slot]);
                                        }
                                    }}
                                    style={{
                                        padding: '8px 12px', borderRadius: 6, border: '1px solid #eee', cursor: 'pointer',
                                        background: selectedTimeSlots.includes(slot) ? '#1890ff' : '#f5f5f5',
                                        color: selectedTimeSlots.includes(slot) ? '#fff' : '#666',
                                        flex: '1 0 calc(20% - 10px)', // 大约一行5个
                                        minWidth: '90px',
                                        textAlign: 'center',
                                        transition: 'all 0.2s'
                                    }}
                                >
                                    {slot}
                                </button>
                            ))}
                        </div>
                    </div>
                </div>

                {/* 场地选择 */}
                <div style={{ marginTop: 20 }}>
                    <label style={{ display: 'block', marginBottom: 8, fontWeight: 'bold', fontSize: 13, color: '#666' }}>
                        优先场地 (建议多选)
                    </label>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(8, 1fr)', gap: 8 }}>
                        {venueList.map(v => (
                            <button
                                key={v.id}
                                onClick={() => {
                                    if (selectedVenues.includes(v.id)) setSelectedVenues(selectedVenues.filter(id => id !== v.id));
                                    else setSelectedVenues([...selectedVenues, v.id]);
                                }}
                                style={{
                                    padding: '8px 5px', borderRadius: 6, border: '1px solid #eee', cursor: 'pointer', fontSize: 13,
                                    background: selectedVenues.includes(v.id) ? '#e6f7ff' : '#fff',
                                    color: selectedVenues.includes(v.id) ? '#1890ff' : '#666',
                                    borderColor: selectedVenues.includes(v.id) ? '#91d5ff' : '#eee'
                                }}
                            >
                                {v.name}
                            </button>
                        ))}
                    </div>
                </div>

                <div style={{ marginTop: 25, textAlign: 'right' }}>
                    <button
                        onClick={handleSubmit}
                        disabled={loading}
                        style={{
                            padding: '12px 30px', background: '#1890ff', color: '#fff', border: 'none',
                            borderRadius: 10, fontSize: 15, fontWeight: 'bold', cursor: loading ? 'wait' : 'pointer',
                            opacity: loading ? 0.7 : 1
                        }}
                    >
                        {loading ? '提交中...' : '创建预定任务'}
                    </button>
                </div>
            </div>

            {/* 任务列表 */}
            <h3 style={{ marginBottom: 15, color: '#333' }}>
                我的预约任务 ({monthlyTasks.length})
                <button
                    onClick={fetchMonthlyTasks}
                    style={{ float: 'right', fontSize: 14, background: 'none', border: 'none', color: '#1890ff', cursor: 'pointer' }}
                >
                    🔄 刷新状态
                </button>
            </h3>

            {monthlyTasks.length === 0 ? (
                <div style={{ textAlign: 'center', padding: 40, color: '#999', background: '#f9f9f9', borderRadius: 12 }}>
                    暂无月场预定任务
                </div>
            ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 15 }}>
                    {monthlyTasks.map(task => (
                        <MonthlyTaskCard key={task.task_id} task={task} onCancel={() => handleCancel(task.task_id)} />
                    ))}
                </div>
            )}


            {/* 说明 */}
            <div style={{ marginTop: 40, padding: 20, background: '#fffbe6', borderRadius: 12, border: '1px solid #ffe58f', color: '#d48806', fontSize: 13, lineHeight: 1.6 }}>
                <strong>⚠️ 注意事项：</strong><br />
                1. 月场预定将在每月最后一天 17:59:50 自动启动。<br />
                2. 为保证成功率，Token 需要保持有效。建议在执行当天重新登录一次。<br />
                3. 系统会同时并发请求所有勾选的场地，只要有一个成功就会停止其他请求。<br />
                4. 请确保您的账户余额充足，以免支付失败。
            </div>
        </div>
    );
};

// ==========================================
// MonthlyTaskCard 组件
// ==========================================

const MonthlyTaskCard = ({ task, onCancel }: { task: any, onCancel: () => void }) => {
    const getStatusColor = (s: string) => {
        if (s === 'success') return '#52c41a';
        if (s === 'failed') return '#ff4d4f';
        if (s === 'running') return '#1890ff';
        return '#faad14'; // waiting/pending
    };

    const getStatusText = (s: string) => {
        if (s === 'success') return '预定成功';
        if (s === 'failed') return '预定失败';
        if (s === 'running') return '正在抢购';
        if (s === 'waiting') return '等待执行';
        return '等待中';
    };

    return (
        <div style={{ background: '#fff', borderRadius: 12, padding: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'center', boxShadow: '0 2px 8px rgba(0,0,0,0.05)' }}>
            <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                    <div style={{ background: getStatusColor(task.status), width: 8, height: 8, borderRadius: '50%' }}></div>
                    <span style={{ fontWeight: 'bold', fontSize: 16 }}>{task.target_year}年{task.target_month}月 周{['一', '二', '三', '四', '五', '六', '日'][task.weekday - 1]}</span>
                    <span style={{ fontSize: 13, color: '#999', background: '#f5f5f5', padding: '2px 8px', borderRadius: 4 }}>{getStatusText(task.status)}</span>
                </div>
                <div style={{ fontSize: 13, color: '#666', display: 'flex', gap: 15 }}>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><Clock size={14} /> {task.start_time}-{task.end_time}</span>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><MapPin size={14} /> {task.venue_ids.length}个备选场地</span>
                </div>
                <div style={{ marginTop: 8, fontSize: 12, color: '#999' }}>
                    创建于: {task.created_at}
                </div>
            </div>

            {task.status !== 'success' && task.status !== 'failed' && (
                <button
                    onClick={onCancel}
                    style={{
                        background: '#fff1f0', color: '#ff4d4f', border: '1px solid #ffccc7',
                        padding: '8px 15px', borderRadius: 6, cursor: 'pointer', fontSize: 13,
                        display: 'flex', alignItems: 'center', gap: 5
                    }}
                >
                    <Trash2 size={14} /> 取消任务
                </button>
            )}
        </div>
    );
};


// --- Main App ---

const App = () => {
    const [view, setView] = useState<'login' | 'dashboard' | 'monthly'>('login');

    const [username, setUsername] = useState('202421003514');
    const [password, setPassword] = useState('20030611y$Y');
    const [email, setEmail] = useState('1696725502@qq.com'); // 用户邮箱

    const [status, setStatus] = useState<'idle' | 'checking' | '2fa_needed' | 'success' | 'error' | 'fetching_data' | 'reconnecting'>('idle');
    const [errorMsg, setErrorMsg] = useState('');

    // New: access denied modal state
    const [showAccessDenied, setShowAccessDenied] = useState(false);

    // Logs State
    const [logs, setLogs] = useState<string[]>([]);

    const [token, setToken] = useState<string | null>(() => {
        // 从 localStorage 恢复 token
        try {
            return localStorage.getItem('scut_venue_token');
        } catch { return null; }
    });
    const [codeValue, setCodeValue] = useState('');

    // Data State
    const [allVenueData, setAllVenueData] = useState<VenueCache>({});
    const [tasks, setTasks] = useState<Record<string, TaskInfo>>({});

    // Date Handling
    const [dateOptions, setDateOptions] = useState<{ date: string, label: string }[]>([]);
    const [selectedDate, setSelectedDate] = useState<string>("");

    // Modal State
    const [selectedCell, setSelectedCell] = useState<{ venue: VenueRow, time: string, session: VenueSession } | null>(null);

    // Sniper Config
    const [sniperDate, setSniperDate] = useState("");
    const [sniperTime, setSniperTime] = useState(TIME_SLOTS[TIME_SLOTS.length - 1]);
    const [sniperLockMode, setSniperLockMode] = useState(false);

    const [autoRefresh, setAutoRefresh] = useState(false);
    const [reconnectCountDown, setReconnectCountDown] = useState(0);

    // 救援 2FA 相关状态
    const [rescueNeed2FA, setRescueNeed2FA] = useState(false);
    const [rescue2FACode, setRescue2FACode] = useState('');

    // 初始化日期
    useEffect(() => {
        const opts = [];
        const today = new Date();
        for (let i = 0; i < 8; i++) {
            const d = new Date(today);
            d.setDate(today.getDate() + i);
            const yyyy = d.getFullYear();
            const mm = String(d.getMonth() + 1).padStart(2, '0');
            const dd = String(d.getDate()).padStart(2, '0');
            const dateStr = `${yyyy}-${mm}-${dd}`;
            const weekday = WEEKDAYS[d.getDay()];
            opts.push({ date: dateStr, label: `${mm}-${dd} (${weekday})` });
        }
        setDateOptions(opts);
        setSelectedDate(opts[0].date);
        setSniperDate(opts[0].date);
    }, []);

    // 持久化 token 到 localStorage
    useEffect(() => {
        try {
            if (token) {
                localStorage.setItem('scut_venue_token', token);
            } else {
                localStorage.removeItem('scut_venue_token');
            }
        } catch { }
    }, [token]);

    // 页面加载时，如果有缓存的 token，自动切换到 dashboard 并获取数据
    useEffect(() => {
        if (token && view === 'login') {
            console.log('[DEBUG] Found cached token, auto-resuming to dashboard...');
            setView('dashboard');
            setStatus('fetching_data');
            fetchAllWeekData(token).then(() => {
                console.log('[DEBUG] Auto-resume successful');
            }).catch((e) => {
                console.error('[DEBUG] Auto-resume failed, clearing token:', e);
                setToken(null);
                setView('login');
                setStatus('idle');
            });
        }
    }, []); // 只在首次加载时执行

    // 轮询日志
    useEffect(() => {
        const interval = setInterval(() => {
            fetchLogs();
            if (view === 'dashboard') fetchTasks();
        }, 1000);
        return () => clearInterval(interval);
    }, [view, username]);

    // 自动刷新
    useEffect(() => {
        let interval: any;
        if (autoRefresh && view === 'dashboard' && token) {
            interval = setInterval(() => {
                fetchAllWeekData(token, false);
            }, 5 * 60 * 1000);
        }
        return () => clearInterval(interval);
    }, [autoRefresh, view, token]);

    const fetchLogs = async () => {
        try {
            const url = username
                ? `${API_BASE_URL}/logs?username=${username}&t=${Date.now()}`
                : `${API_BASE_URL}/logs?t=${Date.now()}`;
            const res = await fetch(url);
            const data = await res.json();
            if (Array.isArray(data)) setLogs(data);
        } catch (e) { }
    };

    const fetchTasks = async () => {
        try {
            const url = username
                ? `${API_BASE_URL}/tasks?username=${username}`
                : `${API_BASE_URL}/tasks`;
            const res = await fetch(url);
            const data = await res.json();
            setTasks(data);
        } catch (e) { }
    };

    const stopTask = async (taskId: string) => {
        try {
            await fetch(`${API_BASE_URL}/task/stop`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ taskId })
            });
            // fetchTasks();
            const newTasks = { ...tasks };
            delete newTasks[taskId];
            setTasks(newTasks);
        } catch (e) {
            alert("停止失败");
        }
    };

    const handleReLogin = async () => {
        setStatus('reconnecting');
        setReconnectCountDown(10);

        // 模拟一个简单的倒计时动画，其实后台在跑登录
        let count = 3;
        const timer = setInterval(() => {
            count--;
            if (count < 0) clearInterval(timer);
        }, 1000);

        try {
            const res = await fetch(`${API_BASE_URL}/login`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            });
            const data = await res.json();

            if (data.status === 'success') {
                setToken(data.token);
                // 登录成功后立即刷新数据
                await fetchAllWeekData(data.token, true);
            } else {
                // 如果自动重连还需要验证码，可能比较麻烦，这里简单处理为回到登录页
                alert("自动重连需要验证码或失败，请手动登录");
                setView('login');
                setStatus('idle');
            }
        } catch (e) {
            alert("重连失败，网络错误");
            setView('login');
            setStatus('idle');
        }
    };

    const fetchAllWeekData = async (authToken: string, showLoading = true) => {
        if (showLoading) setStatus('fetching_data');
        try {
            // 关键修复：发送客户端的日期给后端，解决服务器时间(2026)错误的问题
            // 使用浏览器本地时间（假设用户电脑时间是准的2025年）
            const today = new Date();
            const yyyy = today.getFullYear();
            const mm = String(today.getMonth() + 1).padStart(2, '0');
            const dd = String(today.getDate()).padStart(2, '0');
            const startDateStr = `${yyyy}-${mm}-${dd}`;

            const response = await fetch(`${API_BASE_URL}/venues?token=${encodeURIComponent(authToken)}&startDate=${startDateStr}`);
            const json = await response.json();

            // 新增：检测救援时需要 2FA
            if (json.status === 'need_rescue_2fa') {
                console.log("Rescue needs 2FA, showing modal...");
                setRescueNeed2FA(true);
                setStatus('success'); // 保持界面可用
                return;
            }

            // 关键：检测 Token 是否失效
            // 如果后端返回错误，或者所有日期的数据都是空的（虽然不太可能，但防一手），且不是网络问题
            if (json.error || (json.code && json.code !== 200)) {
                console.warn("Token expired or invalid response, triggering re-login...");
                handleReLogin();
                return;
            }

            // 另一种情况，如果数据全是空的，可能 token 过期导致鉴权失败返回了空列表
            const hasData = Object.values(json).some((dayData: any) => dayData.length > 0);
            if (!hasData && Object.keys(json).length > 0) {
                // 这是一个策略选择：如果没有任何数据，怀疑是 Token 死了，尝试重连
                // 但也可能是真的没数据。为了稳妥，这里我们主要依赖 json.error
            }

            setAllVenueData(json);
            setStatus('success');
        } catch (e: any) {
            setStatus('error');
        }
    };

    // 救援 2FA 提交函数
    const submitRescue2FA = async () => {
        if (!rescue2FACode) return;
        try {
            const res = await fetch(`${API_BASE_URL}/submit_2fa`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: rescue2FACode, username: username })
            });
            const data = await res.json();
            if (data.status === 'success') {
                // 更新 token
                setToken(data.token);
                setRescueNeed2FA(false);
                setRescue2FACode('');
                // 重新获取数据
                await fetchAllWeekData(data.token);
            } else {
                alert(`验证失败: ${data.msg}`);
            }
        } catch (e: any) {
            alert(`请求失败: ${e.message}`);
        }
    };


    const handleDirectBooking = async () => {
        if (!selectedCell || !token) return;
        const { venue, time, session } = selectedCell;
        const [start, end] = time.split('-');

        const payload = {
            token,
            date: selectedDate,
            startTime: start,
            endTime: end,
            venueId: session.venueId,
            price: session.price,
            stadiumId: session.stadiumId || 1,
            email: email, // 传递邮箱
            username: username // NEW: 传递前端登录的用户名，用于邮件显示
        };

        setSelectedCell(null);

        try {
            const res = await fetch(`${API_BASE_URL}/book/direct`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.status === 'success') {
                alert(`预定成功！邮件通知将发送至 ${email}`);
                // 5秒后自动刷新数据
                setTimeout(() => {
                    fetchAllWeekData(token, false);
                    // 如果订单窗口开着，也许也想刷新订单？可以但没必要太复杂。
                }, 5000);
                fetchAllWeekData(token);
            } else {
                alert(`操作失败: ${data.msg}`);
            }
        } catch (e: any) { }
    };

    const handleLockBooking = async () => {
        if (!selectedCell || !token) return;
        const { venue, time, session } = selectedCell;
        const [start, end] = time.split('-');

        // 关键：把用户点击的“具体场地”信息完整传给后端
        // - venueId：用于后端精准锁定该场地（不再选“第一个可预约”）
        // - venueName：用于后端日志/邮件提示（可选，但建议）
        // - stadiumId：与查询/下单保持一致（可选，但建议）
        const payload = {
            token,
            date: selectedDate,
            startTime: start,
            endTime: end,
            lockMode: true,
            // 价格建议使用该格子的真实价格（避免后端校验不一致）
            price: session.price,
            email: email,
            username: username,
            venueId: session.venueId,
            venueName: venue.name,
            stadiumId: session.stadiumId || 1
        };

        setSelectedCell(null);

        try {
            await fetch(`${API_BASE_URL}/task/monitor`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            fetchTasks();
            setTimeout(() => fetchAllWeekData(token, false), 5000);
        } catch (e: any) {
            alert(e.message);
        }
    };

    const handleStartMonitor = async () => {
        if (!token) return;
        const [start, end] = sniperTime.split('-');
        const payload = {
            token,
            date: sniperDate,
            startTime: start,
            endTime: end,
            lockMode: sniperLockMode,
            price: 40,
            email: email, // 传递邮箱
            username: username // NEW: 传递前端登录的用户名
        };
        try {
            await fetch(`${API_BASE_URL}/task/monitor`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            fetchTasks();
        } catch (e: any) {
            alert(e.message);
        }
    };

    const handleLogin = async () => {
        console.log("[DEBUG] handleLogin called, status:", status, "username:", username);
        if (!username || !password) {
            console.log("[DEBUG] Missing username or password");
            return;
        }
        setStatus('checking');

        // Explicitly show the URL for debugging
        const url = `${API_BASE_URL}/login`;
        console.log("[DEBUG] Sending login request to:", url);

        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password, email }) // Pass email as well to update session cache
            });
            const data = await res.json();

            if (data.status === 'success') {
                console.log("[DEBUG] Login successful, token received, switching to dashboard...");
                // 1. 先更新Token
                setToken(data.token);

                // 2. 立即切换到 dashboard 视图
                setView('dashboard');
                setStatus('fetching_data');
                console.log("[DEBUG] View set to dashboard, fetching venue data...");

                try {
                    await fetchAllWeekData(data.token);
                    console.log("[DEBUG] Venue data fetched successfully");
                } catch (e) {
                    console.error("[DEBUG] Failed to fetch venue data:", e);
                    // 即使获取数据失败，也保持在dashboard，用户可以手动刷新
                }
            } else if (data.status === 'need_2fa') {
                setStatus('2fa_needed');
            } else if (data.status === 'forbidden') {
                // 处理白名单拦截
                setShowAccessDenied(true);
                setStatus('idle');
            } else {
                throw new Error(data.msg);
            }
        } catch (e: any) {
            console.error("Login Error:", e);
            setErrorMsg(`请求失败 (${url}): ${e.message}`);
            setStatus('error');
        }
    };

    const verify2FA = async () => {
        setStatus('checking');
        try {
            // Pass username to identify which driver to use
            const res = await fetch(`${API_BASE_URL}/submit_2fa`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: codeValue, username: username })
            });
            const data = await res.json();
            if (data.status === 'success') {
                setToken(data.token);
                // 与 handleLogin 保持一致：先切换到dashboard，再获取数据
                setView('dashboard');
                setStatus('fetching_data');
                try {
                    await fetchAllWeekData(data.token);
                } catch (e) {
                    console.error("Failed to fetch venue data:", e);
                }
            } else {
                throw new Error(data.msg);
            }
        } catch (e: any) {
            setErrorMsg(e.message);
            setStatus('error');
        }
    };

    // --- NEW: Handle Logout properly ---
    const handleLogout = () => {
        console.log("[DEBUG] handleLogout called");
        setToken(null);
        setAllVenueData({});
        setStatus('idle'); // 关键：重置状态，否则登录按钮不显示
        setErrorMsg(''); // 清空错误信息
        setCodeValue(''); // 清空2FA验证码
        setView('login');
        setLogs([]);
        console.log("[DEBUG] Logout complete, status reset to idle");
    };

    return (
        <div style={{ minHeight: '100vh', background: '#f0f2f5', fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif' }}>
            {status === 'reconnecting' && <LoadingOverlay message="检测到Token失效，正在自动重连..." />}

            {/* 白名单拦截弹窗 */}
            <AccessDeniedModal isOpen={showAccessDenied} onClose={() => setShowAccessDenied(false)} />

            {/* 救援 2FA 弹窗 */}
            <Rescue2FAModal
                isOpen={rescueNeed2FA}
                code={rescue2FACode}
                setCode={setRescue2FACode}
                onSubmit={submitRescue2FA}
                onClose={() => { setRescueNeed2FA(false); setRescue2FACode(''); }}
            />

            {view === 'login' ? (
                <LoginView
                    username={username} setUsername={setUsername}
                    password={password} setPassword={setPassword}
                    email={email} setEmail={setEmail}
                    handleLogin={handleLogin}
                    status={status}
                    errorMsg={errorMsg}
                    verify2FA={verify2FA}
                    codeValue={codeValue} setCodeValue={setCodeValue}
                    logs={logs}
                    showAccessDenied={showAccessDenied} setShowAccessDenied={setShowAccessDenied}
                    loginMsg="正在登录..."
                />
            ) : view === 'monthly' ? (
                <MonthlyBookingView
                    username={username}
                    token={token!}
                    setView={setView}
                    tasks={Object.values(tasks).filter((t: any) => t.type === 'monthly')} // 只传递月场任务，需要后端支持或在这里过滤
                    fetchTasks={fetchTasks}
                />
            ) : (
                <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    <DashboardView
                        autoRefresh={autoRefresh} setAutoRefresh={setAutoRefresh}
                        fetchAllWeekData={fetchAllWeekData} token={token} setView={setView}
                        dateOptions={dateOptions} selectedDate={selectedDate} setSelectedDate={setSelectedDate}
                        status={status} allVenueData={allVenueData} setSelectedCell={setSelectedCell}

                        // Sniper Props
                        sniperDate={sniperDate} setSniperDate={setSniperDate}
                        sniperTime={sniperTime} setSniperTime={setSniperTime}
                        sniperLockMode={sniperLockMode} setSniperLockMode={setSniperLockMode}
                        handleStartMonitor={handleStartMonitor}
                        logs={logs}

                        // Tasks
                        tasks={tasks} fetchTasks={fetchTasks} stopTask={stopTask}

                        // Modal
                        selectedCell={selectedCell}
                        handleDirectBooking={handleDirectBooking}
                        handleLockBooking={handleLockBooking}

                        // New Props
                        username={username}
                        handleLogout={handleLogout} // Pass it down
                    />
                </div>
            )}
        </div>
    );
};

const root = createRoot(document.getElementById('root')!);
root.render(<App />);