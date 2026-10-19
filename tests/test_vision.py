"""Shelf camera logic with stand-in YOLO models, plus the engine's camera-vs-weight fusion."""
import pytest

from server.catalog import Catalog
from server.config import ROOT, Settings
from server.engine import Engine
from server.ledger import Ledger
from server.payments import SandboxPayments
from server.security import TokenSigner
from shelf_station.vision import Evidence, ProductRecognizer, Sighting

np = pytest.importorskip("numpy")


# ---------------------------------------------------------------- evidence voting
def test_evidence_picks_the_product_most_frames_agree_on():
    ev = Evidence()
    for t, sku, conf in [(1, "razor", .9), (1.2, "razor", .8), (1.4, "blades", .6), (1.6, "razor", .85)]:
        ev.add(Sighting(t, sku, conf, True))
    seen = ev.around(2.0)
    assert seen["sku"] == "razor" and seen["hand_seen"] is True
    assert 0.6 < seen["confidence"] < 0.85  # mean 0.85, discounted for the one dissenting frame


def test_evidence_ignores_old_frames_and_reports_hand_without_product():
    ev = Evidence()
    ev.add(Sighting(0, "razor", .9, True))
    ev.add(Sighting(9, None, 0, True))
    seen = ev.around(10.0)
    assert seen["sku"] is None and seen["hand_seen"] is True


# ---------------------------------------------------------------- recognizer, stand-in models
class _Probs:
    def __init__(self, top1, conf):
        self.top1, self.top1conf = top1, conf


class _Result:
    def __init__(self, probs=None, boxes=None):
        self.probs, self.boxes = probs, boxes


class FakeClassifier:
    task = "classify"
    names = {0: "blades", 1: "empty", 2: "razor"}

    def __init__(self, answers):
        self.answers, self.seen_shapes = answers, []

    def __call__(self, images, verbose=False):
        self.seen_shapes = [im.shape for im in images]
        return [_Result(_Probs(*a)) for a in self.answers[:len(images)]]


class _List(list):
    def tolist(self):
        return list(self)


class _Boxes:
    def __init__(self, rows):
        self.xyxy = _List([r[0] for r in rows])
        self.cls = _List([r[1] for r in rows])
        self.conf = _List([r[2] for r in rows])


class FakeDetector:
    task = "detect"
    names = ["razor", "blades"]

    def __init__(self, rows):
        self.rows = rows

    def __call__(self, frame, verbose=False):
        return [_Result(boxes=_Boxes(self.rows))]


def test_classifier_looks_only_around_each_hand():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    model = FakeClassifier([(2, 0.93), (1, 0.99)])  # hand 1 holds a razor, hand 2 is empty
    rec = ProductRecognizer(model, min_confidence=0.5)
    found = rec.recognize(frame, [(100, 100, 160, 180), (400, 200, 450, 260)])
    assert found == [("razor", 0.93)]
    assert all(h < 480 and w < 640 for h, w, _ in model.seen_shapes)


def test_classifier_drops_low_confidence_and_maps_class_names():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert ProductRecognizer(FakeClassifier([(0, 0.3)]), min_confidence=0.5).recognize(frame) == []
    mapped = ProductRecognizer(FakeClassifier([(0, 0.8)]), {"blades": "blades-4pk"}).recognize(frame)
    assert mapped == [("blades-4pk", 0.8)]


def test_detector_keeps_only_boxes_near_a_hand():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    rows = [((110, 110, 150, 170), 0, 0.88),   # razor in the hand
            ((500, 20, 560, 80), 1, 0.95)]     # blades still sitting on the shelf, no hand near
    found = ProductRecognizer(FakeDetector(rows)).recognize(frame, [(100, 100, 160, 180)])
    assert found == [("razor", 0.88)]


# ---------------------------------------------------------------- engine fusion
@pytest.fixture
def engine(tmp_path):
    e = Engine(Settings(), Catalog.load(ROOT / "data" / "catalog.json"), Ledger(":memory:"),
               TokenSigner(tmp_path / "k.pem"), SandboxPayments())
    sess, _, _ = e.start_session(None, None, True, 0)
    e.set_location(sess.session_id, "7", 0)
    return e


def last_pick(e):
    return list(e.picks.values())[-1]


def test_camera_confirms_the_lane_product(engine):
    engine.shelf_event("st", "S7-A", "take", 1, lane=0, weight_delta_g=-52, vision_sku="razor", vision_confidence=0.9)
    assert last_pick(engine).sku == "razor" and "confirmed" in last_pick(engine).evidence


def test_camera_overrides_a_misplaced_item_when_weight_fits(engine):
    # Lane 0 is razors (52 g) but a blade pack (40 g) was left there; camera sees blades.
    notes = engine.shelf_event("st", "S7-A", "take", 1, lane=0, weight_delta_g=-40,
                               vision_sku="blades", vision_confidence=0.92)
    assert last_pick(engine).sku == "blades"
    assert any(n.kind == "feed" and "misplaced" in n.data["text"] for n in notes)


def test_camera_is_overruled_when_weight_contradicts_it(engine):
    engine.shelf_event("st", "S7-A", "take", 1, lane=2, weight_delta_g=-160,  # a chocolate's weight
                       vision_sku="blades", vision_confidence=0.9)
    assert last_pick(engine).sku == "chocolate" and "disagreed" in last_pick(engine).evidence


def test_unsure_camera_is_ignored_and_agreement_rate_is_monitored(engine):
    engine.shelf_event("st", "S7-A", "take", 1, lane=0, weight_delta_g=-52, vision_sku="blades", vision_confidence=0.3)
    assert last_pick(engine).sku == "razor"
    for i in range(9):
        engine.shelf_event("st", "S7-A", "take", 2 + i, lane=0, weight_delta_g=-52,
                           vision_sku="razor", vision_confidence=0.9)
    engine.shelf_event("st", "S7-A", "take", 20, lane=2, weight_delta_g=-160, vision_sku="blades", vision_confidence=0.9)
    engine.shelf_event("st", "S7-A", "take", 21, lane=2, weight_delta_g=-160, vision_sku="razor", vision_confidence=0.9)
    vision = engine.summary(30, True)["vision"]
    assert vision["low_confidence"] == 1 and vision["agree"] == 9 and vision["disagree"] == 2
    assert any("Camera and weight disagree" in a["text"] for a in engine.summary(30, True)["alerts"])


def test_hailo_classifier_plugs_into_the_recognizer(tmp_path):
    """The Hailo backend (HEF on the Pi's accelerator) behaves like an Ultralytics classify model."""
    import json

    from shelf_station.hailo import HailoClassifier, softmax_if_needed, square_rgb
    from shelf_station.vision import ProductRecognizer

    seen = []

    def fake_hailo(batch):  # uint8 NHWC in, raw scores out (as a HEF without the final softmax gives)
        seen.append(batch.shape)
        return np.array([[0.1, 4.0, 0.2]] * len(batch))

    (tmp_path / "m.names.json").write_text(json.dumps(["razor", "blades", "empty"]))
    clf = HailoClassifier(tmp_path / "m.hef", runner=fake_hailo, size=32)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    found = ProductRecognizer(clf, min_confidence=0.5).recognize(frame)
    assert found and found[0][0] == "blades" and found[0][1] > 0.9
    assert seen == [(1, 32, 32, 3)]
    assert square_rgb(np.zeros((10, 30, 3), np.uint8), 8).shape == (8, 8, 3)
    probs = np.array([0.2, 0.7, 0.1])
    assert np.allclose(softmax_if_needed(probs), probs)  # already probabilities: left alone
