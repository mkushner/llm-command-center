interface Props {
  prompt: string;
  detail?: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

export function ConfirmModal({
  prompt,
  detail,
  confirmLabel = "Confirm",
  danger,
  onConfirm,
  onClose,
}: Props) {
  return (
    <div className="scrim" onMouseDown={onClose}>
      <div className="modal confirm" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-bd">
          <div className="confirm-title">{prompt}</div>
          {detail && <div className="confirm-detail">{detail}</div>}
        </div>
        <div className="modal-ft">
          <button onClick={onClose}>Cancel</button>
          <button
            className={danger ? "danger" : "primary"}
            autoFocus
            onClick={() => {
              onConfirm();
              onClose();
            }}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
