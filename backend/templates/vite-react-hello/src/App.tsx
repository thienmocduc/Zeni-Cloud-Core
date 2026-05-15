import { useState } from 'react';

const FEATURES = [
  'Lightning-fast dev with native ESM',
  'React 18 + TypeScript out of the box',
  'Multi-stage Docker build → tiny nginx image',
  'Auto-scale on Zeni Cloud Run',
  'Edit src/App.tsx and redeploy'
];

export default function App() {
  const [count, setCount] = useState(0);
  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'linear-gradient(135deg,#0f0f23,#1a1a3e)',
      color: '#fff',
      fontFamily: 'system-ui, -apple-system, sans-serif',
      padding: 24
    }}>
      <div style={{ maxWidth: 720, textAlign: 'center' }}>
        <div style={{ fontSize: 64, marginBottom: 12 }}>⚛️</div>
        <h1 style={{ fontSize: 36, color: '#22d3ee', margin: 0 }}>
          Vite + React on Zeni Cloud
        </h1>
        <p style={{ color: '#cbd5e1', marginTop: 12 }}>
          Your single-page app is live. Replace src/App.tsx to ship your UI.
        </p>
        <button
          onClick={() => setCount(c => c + 1)}
          style={{
            marginTop: 16, padding: '10px 22px',
            background: '#7d68ff', color: '#fff',
            border: 0, borderRadius: 8, fontSize: 14, cursor: 'pointer'
          }}>
          Counter: {count}
        </button>
        <ul style={{
          listStyle: 'none', padding: 0, textAlign: 'left',
          background: 'rgba(255,255,255,0.04)',
          border: '1px solid rgba(34,211,238,0.3)',
          borderRadius: 12, padding: 24, marginTop: 28
        }}>
          {FEATURES.map(f => (
            <li key={f} style={{ padding: '6px 0', color: '#e2e8f0' }}>
              <span style={{ color: '#22d3ee', marginRight: 8 }}>✓</span>{f}
            </li>
          ))}
        </ul>
        <p style={{ fontSize: 12, color: '#64748b', marginTop: 24 }}>
          Powered by <strong style={{ color: '#22d3ee' }}>Zeni Cloud</strong> · Vite + React
        </p>
      </div>
    </div>
  );
}
