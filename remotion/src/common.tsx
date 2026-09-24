import React from 'react';
import {Audio, Easing, Img, Sequence, interpolate, staticFile} from 'remotion';

export const W = 1080, H = 1920, A = H / W, M = 72;
export const INK = '#F2EEE6', MUTE = 'rgba(242,238,230,.62)';

export const Fonts: React.FC = () => <style>{`
@font-face{font-family:Serif;src:url(${staticFile('InstrumentSerif-Regular.ttf')})}
@font-face{font-family:Serif;font-style:italic;src:url(${staticFile('InstrumentSerif-Italic.ttf')})}
@font-face{font-family:Grot;src:url(${staticFile('InterTight.ttf')});font-weight:100 900}
@font-face{font-family:Mono;src:url(${staticFile('IBMPlexMono-Regular.ttf')})}
@font-face{font-family:Mono;font-weight:500;src:url(${staticFile('IBMPlexMono-Medium.ttf')})}`}</style>;

export const clamp = {extrapolateLeft: 'clamp' as const, extrapolateRight: 'clamp' as const};
export const out3 = {...clamp, easing: Easing.out(Easing.cubic)};

// разгон и торможение по синусу: скорость нарастает и спадает мягко, без рывка на стыках
export const easeSine = (u: number) => -(Math.cos(Math.PI * Math.min(1, Math.max(0, u))) - 1) / 2;
// камера: [время, cx, cy, ширина кадра, растворение] в пикселях картины. Отрезок, который кончается ключом
// с растворением = 1, — не проезд: кадр предыдущего ключа растворяется в кадре этого
export type Cam = number[];
export type CamState = {cam: [number, number, number]; prev?: [number, number, number]; mix: number};
export function camState(keys: Cam[], t: number): CamState {
  if (!keys.length) return {cam: [0, 0, 1], mix: 1};
  if (t <= keys[0][0]) return {cam: [keys[0][1], keys[0][2], keys[0][3]], mix: 1};
  for (let i = 0; i < keys.length - 1; i++) {
    const a = keys[i], b = keys[i + 1];
    if (t <= b[0]) {
      const u = easeSine((t - a[0]) / Math.max(b[0] - a[0], 1e-6));
      if (b[4] === 1) return {cam: [b[1], b[2], b[3]], prev: [a[1], a[2], a[3]], mix: u};
      return {cam: [a[1] + (b[1] - a[1]) * u, a[2] + (b[2] - a[2]) * u,
        Math.exp(Math.log(a[3]) + (Math.log(b[3]) - Math.log(a[3])) * u)], mix: 1};
    }
  }
  const l = keys[keys.length - 1];
  return {cam: [l[1], l[2], l[3]], mix: 1};
}
export const camAt = (keys: Cam[], t: number) => camState(keys, t).cam;

// кадр не выходит за картину (с запасом slack экранных px) по тем осям, где он её меньше
export function clampCam(pw: number, ph: number, c: [number, number, number], slack: number[] = [0, 0]):
  [number, number, number] {
  let [cx, cy, cw] = c; const ch = cw * A, s = W / cw, sx = slack[0] / s, sy = slack[1] / s;
  if (cw <= pw) cx = Math.min(Math.max(cx, cw / 2 - sx), pw - cw / 2 + sx);
  if (ch <= ph) cy = Math.min(Math.max(cy, ch / 2 - sy), ph - ch / 2 + sy);
  return [cx, cy, cw];
}

// картина под камерой на тёмном фоне. Размер — через width/height (браузер сглаживает уменьшение),
// сдвиг — через transform: он не округляется до пикселя, медленный наезд идёт без ступенек
export const Stage: React.FC<{src: string; pw: number; ph: number; cam: [number, number, number];
  shadow?: number; blur?: [number, number]; id?: string}> = ({src, pw, ph, cam, shadow = 0, blur, id = 'mb'}) => {
  const [cx, cy, cw] = cam; const s = W / cw, ch = cw * A;
  // смаз по направлению движения — только на быстром участке проезда
  const on = !!blur && (blur[0] > 0.3 || blur[1] > 0.3);
  return <>
    {on && <svg width={0} height={0} style={{position: 'absolute'}}><filter id={id} x="-5%" y="-5%" width="110%" height="110%">
      <feGaussianBlur stdDeviation={`${blur![0].toFixed(2)} ${blur![1].toFixed(2)}`} /></filter></svg>}
    <Img src={src} style={{position: 'absolute', left: 0, top: 0, width: pw * s, height: ph * s,
      transform: `translate3d(${-(cx - cw / 2) * s}px, ${-(cy - ch / 2) * s}px, 0)`,
      filter: on ? `url(#${id})` : undefined,
      boxShadow: shadow ? `0 30px 80px rgba(0,0,0,${0.6 * shadow})` : undefined}} />
  </>;
};

// звуки: встроенные (static:имя) или свои из /data/sfx (адрес на локальном сервере)
export const Sfx: React.FC<{list?: any[]; fps: number}> = ({list, fps}) => <>{(list || []).map((e: any, i: number) =>
  <Sequence key={i} from={Math.round(e.t * fps)}>
    <Audio src={String(e.src).startsWith('static:') ? staticFile(String(e.src).slice(7)) : e.src} volume={e.vol} />
  </Sequence>)}</>;

export const Bar: React.FC<{text: string; opacity?: number}> = ({text, opacity = 1}) =>
  <div style={{position: 'absolute', left: M, right: M, top: 150, display: 'flex', alignItems: 'center', gap: 22,
    fontFamily: 'Mono', fontSize: 21, letterSpacing: '.14em', textTransform: 'uppercase', color: MUTE, opacity}}>
    <Img src={staticFile('logo.png')} style={{height: 30, opacity: .9}} />
    <span style={{width: 46, height: 1, background: MUTE}} />
    <span>{text}</span>
  </div>;

export const Grain: React.FC<{frame: number}> = ({frame}) => {
  const g = (frame * 37) % 300;
  return <div style={{position: 'absolute', inset: 0, opacity: .08, mixBlendMode: 'overlay',
    backgroundPosition: `${g}px ${g * 1.7}px`,
    backgroundImage: `url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='300' height='300'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>")`}} />;
};

// *слово* → курсив антиквы
export const Rich: React.FC<{text: string}> = ({text}) => <>{
  text.split(/(\*[^*]+\*)/).map((part, i) => part.startsWith('*') && part.endsWith('*')
    ? <i key={i} style={{fontFamily: 'Serif', fontStyle: 'italic'}}>{part.slice(1, -1)}</i> : <span key={i}>{part}</span>)
}</>;
