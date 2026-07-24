interface Props {
  title: string;
  text: string;
  onClose: () => void;
}

function lineClass(line: string): string {
  if (line.startsWith("+") && !line.startsWith("+++")) return "add";
  if (line.startsWith("-") && !line.startsWith("---")) return "del";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("diff ") || line.startsWith("index ") || line.startsWith("+++") || line.startsWith("---"))
    return "meta";
  return "";
}

export function DiffModal({ title, text, onClose }: Props) {
  const lines = text ? text.split("\n") : [];
  return (
    <div className="scrim" onMouseDown={onClose}>
      <div
        className="modal"
        style={{ maxWidth: 920, width: "92%", height: "84vh", maxHeight: "84vh" }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="modal-hd">
          Diff · {title}
          <span className="hint">Esc close</span>
        </div>
        <div className="diff-body">
          {lines.length === 0 && <span className="c-faint">No changes.</span>}
          {lines.map((l, i) => (
            <div key={i} className={lineClass(l)}>
              {l || " "}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
