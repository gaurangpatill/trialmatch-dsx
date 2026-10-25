"use client";

import {useEffect, useRef} from "react";

type Node = {
  x: number;
  y: number;
  ox: number;
  oy: number;
  r: number;
  phase: number;
  tone: number;
};

type DragState = {
  pointerId: number;
  x: number;
  y: number;
  members: Array<{node: Node; weight: number; startX: number; startY: number}>;
};

export function Constellation() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const cursor = {x: -1000, y: -1000};
    let drag: DragState | null = null;
    let width = 0;
    let height = 0;
    let frame = 0;
    let visible = true;
    let previous = 0;
    let slowFrames = 0;
    let nodes: Node[] = [];
    let seed = 73519;

    const random = () => {
      seed = (seed * 16807) % 2147483647;
      return (seed - 1) / 2147483646;
    };

    const draw = (time: number) => {
      context.clearRect(0, 0, width, height);
      const animationTime = reducedMotion.matches ? 0 : time * 0.00015;
      const tones = ["246,231,168", "154,164,190", "255,253,248"];

      for (const node of nodes) {
        const dx = cursor.x - node.x;
        const dy = cursor.y - node.y;
        const distance = Math.hypot(dx, dy);
        const pull = !reducedMotion.matches && !drag && distance < 180
          ? (1 - distance / 180) * 0.065
          : 0;
        const targetX = node.ox + Math.sin(animationTime + node.phase) * 14 + dx * pull;
        const targetY = node.oy + Math.cos(animationTime * 0.7 + node.phase) * 14 + dy * pull;
        node.x += (targetX - node.x) * 0.035;
        node.y += (targetY - node.y) * 0.035;
      }

      const connectionDistance = width < 640 ? 100 : 162;
      nodes.forEach((node, index) => {
        let connections = 0;
        for (let otherIndex = index + 1; otherIndex < nodes.length && connections < 4; otherIndex += 1) {
          const other = nodes[otherIndex];
          const distance = Math.hypot(node.x - other.x, node.y - other.y);
          if (distance >= connectionDistance) continue;
          const pulse = reducedMotion.matches ? 0.8 : 0.65 + Math.sin(animationTime + node.phase + other.phase) * 0.35;
          context.strokeStyle = `rgba(185,204,220,${(1 - distance / connectionDistance) * 0.34 * pulse})`;
          context.lineWidth = 0.65;
          context.beginPath();
          context.moveTo(node.x, node.y);
          context.lineTo(other.x, other.y);
          context.stroke();
          connections += 1;
        }

        const nearCursor = !reducedMotion.matches && Math.hypot(node.x - cursor.x, node.y - cursor.y) < 160;
        const opacity = 0.4 + (Math.sin(animationTime + node.phase) + 1) * 0.22;
        context.shadowBlur = nearCursor ? 14 : 7;
        context.shadowColor = `rgba(${tones[node.tone]},0.65)`;
        context.fillStyle = `rgba(${tones[node.tone]},${nearCursor ? 1 : opacity})`;
        context.beginPath();
        context.arc(node.x, node.y, node.r * (nearCursor ? 1.3 : 1), 0, Math.PI * 2);
        context.fill();
        context.shadowBlur = 0;
      });
    };

    const resize = () => {
      const bounds = canvas.getBoundingClientRect();
      width = Math.max(1, bounds.width);
      height = Math.max(1, bounds.height);
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      seed = 73519;
      const count = Math.min(290, Math.max(95, Math.round(width * height / 2800)));
      nodes = Array.from({length: count}, (_, index) => {
        const edge = index >= count * 0.75;
        const region = index % 3;
        const x = width * (edge && region === 2 ? 0.78 + random() * 0.21 : 0.01 + random() * 0.98);
        const y = height * (edge && region === 0
          ? 0.02 + random() * 0.18
          : edge && region === 1
            ? 0.8 + random() * 0.18
            : 0.02 + random() * 0.96);
        return {x, y, ox: x, oy: y, r: random() * 1.25 + 0.65, phase: random() * Math.PI * 2, tone: Math.floor(random() * 3)};
      });
      draw(0);
    };

    const tick = (time: number) => {
      if (!visible || document.hidden || reducedMotion.matches) {
        frame = 0;
        return;
      }
      if (time - previous > 32) {
        const started = performance.now();
        draw(time);
        previous = time;
        slowFrames = performance.now() - started > 12 ? slowFrames + 1 : Math.max(0, slowFrames - 1);
        if (slowFrames > 10 && nodes.length > 38) {
          nodes = nodes.slice(0, Math.max(38, Math.floor(nodes.length * 0.8)));
          slowFrames = 0;
        }
      }
      frame = requestAnimationFrame(tick);
    };

    const start = () => {
      cancelAnimationFrame(frame);
      frame = 0;
      if (reducedMotion.matches) draw(0);
      else if (visible && !document.hidden) frame = requestAnimationFrame(tick);
    };

    const pointerPosition = (event: PointerEvent) => {
      const bounds = canvas.getBoundingClientRect();
      return {x: event.clientX - bounds.left, y: event.clientY - bounds.top};
    };

    const move = (event: PointerEvent) => {
      if (event.pointerType === "touch" && !drag) return;
      const position = pointerPosition(event);
      cursor.x = position.x;
      cursor.y = position.y;
      if (drag && event.pointerId === drag.pointerId) {
        const dx = cursor.x - drag.x;
        const dy = cursor.y - drag.y;
        for (const member of drag.members) {
          const x = Math.max(4, Math.min(width - 4, member.startX + dx * member.weight));
          const y = Math.max(4, Math.min(height - 4, member.startY + dy * member.weight));
          member.node.ox = x;
          member.node.oy = y;
          member.node.x += (x - member.node.x) * 0.45;
          member.node.y += (y - member.node.y) * 0.45;
        }
        if (reducedMotion.matches) draw(0);
      }
    };

    const pointerDown = (event: PointerEvent) => {
      const position = pointerPosition(event);
      cursor.x = position.x;
      cursor.y = position.y;
      let nearest: Node | null = null;
      let nearestDistance = 48;
      for (const node of nodes) {
        const distance = Math.hypot(node.x - cursor.x, node.y - cursor.y);
        if (distance < nearestDistance) {
          nearest = node;
          nearestDistance = distance;
        }
      }
      if (!nearest) return;
      const members = nodes.flatMap((node) => {
        const distance = Math.hypot(node.x - nearest.x, node.y - nearest.y);
        if (distance > 140) return [];
        return [{node, weight: Math.max(0.08, 1 - distance / 150), startX: node.ox, startY: node.oy}];
      });
      drag = {pointerId: event.pointerId, x: cursor.x, y: cursor.y, members};
      canvas.dataset.dragging = "true";
      canvas.setPointerCapture(event.pointerId);
      event.preventDefault();
    };

    const endDrag = (event: PointerEvent) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
      drag = null;
      delete canvas.dataset.dragging;
      if (reducedMotion.matches) draw(0);
    };

    const leave = () => {
      if (!drag) {
        cursor.x = -1000;
        cursor.y = -1000;
      }
    };

    const visibilityChanged = () => start();
    const resizeObserver = new ResizeObserver(resize);
    const intersectionObserver = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      start();
    }, {threshold: 0.01});

    resizeObserver.observe(canvas);
    intersectionObserver.observe(canvas);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerdown", pointerDown);
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    canvas.addEventListener("pointerleave", leave);
    document.addEventListener("visibilitychange", visibilityChanged);
    reducedMotion.addEventListener("change", start);
    resize();
    start();

    return () => {
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      intersectionObserver.disconnect();
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerdown", pointerDown);
      canvas.removeEventListener("pointerup", endDrag);
      canvas.removeEventListener("pointercancel", endDrag);
      canvas.removeEventListener("pointerleave", leave);
      document.removeEventListener("visibilitychange", visibilityChanged);
      reducedMotion.removeEventListener("change", start);
    };
  }, []);

  return <canvas ref={canvasRef} className="constellation-canvas" aria-hidden="true" />;
}
