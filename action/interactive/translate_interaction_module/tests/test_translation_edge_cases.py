#!/usr/bin/env python3
"""
Test script for Edge Cases in Ensemble Language Detection
Tests short single-word text and longer paragraph text.
"""
import asyncio
from services.translation_providers.ensemble_detector import EnsembleLanguageDetector

# Short single-word/phrase tests (may be too short for detection)
SHORT_MESSAGES = [
    ("Hola", "es", "Spanish - single word"),
    ("Bonjour", "fr", "French - single word"),
    ("Guten Tag", "de", "German - greeting"),
    ("Ciao", "it", "Italian - single word"),
    ("Привет", "ru", "Russian - single word"),
    ("こんにちは", "ja", "Japanese - greeting"),
    ("你好", "zh", "Chinese - greeting"),
    ("안녕하세요", "ko", "Korean - greeting"),
    ("مرحبا", "ar", "Arabic - greeting"),
    ("Olá", "pt", "Portuguese - single word"),
]

# Longer paragraph tests (should be highly accurate)
LONG_MESSAGES = [
    (
        "@penguinzplays I've been watching your streams for over two years now and I have to say, "
        "your content keeps getting better and better. The community you've built is amazing and "
        "I love how you interact with your viewers. Keep up the great work!",
        "en", "English - long paragraph"
    ),
    (
        "@penguinzplays He estado viendo tus transmisiones durante más de dos años y tengo que decir "
        "que tu contenido sigue mejorando cada vez más. La comunidad que has construido es increíble "
        "y me encanta cómo interactúas con tus espectadores. ¡Sigue así!",
        "es", "Spanish - long paragraph"
    ),
    (
        "@penguinzplays Je regarde tes streams depuis plus de deux ans maintenant et je dois dire que "
        "ton contenu s'améliore de plus en plus. La communauté que tu as créée est incroyable et "
        "j'adore comment tu interagis avec tes spectateurs. Continue comme ça!",
        "fr", "French - long paragraph"
    ),
    (
        "@penguinzplays Ich schaue deine Streams seit über zwei Jahren und muss sagen, dass dein "
        "Content immer besser wird. Die Community, die du aufgebaut hast, ist unglaublich und ich "
        "liebe es, wie du mit deinen Zuschauern interagierst. Mach weiter so!",
        "de", "German - long paragraph"
    ),
    (
        "@penguinzplays 私はあなたの配信を二年以上見ています。コンテンツはどんどん良くなっていますね。"
        "あなたが築いたコミュニティは素晴らしいです。視聴者との交流も大好きです。これからも頑張ってください！",
        "ja", "Japanese - long paragraph"
    ),
    (
        "@penguinzplays 我已经看了你的直播两年多了，你的内容越来越好。你建立的社区非常棒，"
        "我喜欢你与观众互动的方式。继续保持！",
        "zh", "Chinese - long paragraph"
    ),
]

# Mixed content tests (emotes, URLs, mentions mixed with text)
MIXED_MESSAGES = [
    (
        "@penguinzplays PogChamp KEKW that play was insane! Check out https://clips.twitch.tv/example LUL",
        "en", "English - with emotes/URL"
    ),
    (
        "@penguinzplays Kappa eso fue increíble! Me encantó el stream de hoy OMEGALUL",
        "es", "Spanish - with emotes"
    ),
    (
        "!points @penguinzplays @someuser Bonjour tout le monde, comment allez-vous aujourd'hui?",
        "fr", "French - with commands/mentions"
    ),
]


async def run_tests():
    """Run all edge case tests."""
    detector = EnsembleLanguageDetector()
    await detector._initialize_models()

    print("=" * 80)
    print("ENSEMBLE LANGUAGE DETECTION - EDGE CASE TESTS")
    print("=" * 80)
    print()
    print("DETECTOR STATUS:")
    print(f"  FastText:   {'✓ Available' if detector.fasttext_model else '✗ Not available'}")
    print(f"  Lingua:     {'✓ Available' if detector.lingua_detector else '✗ Not available'}")
    print(f"  Langdetect: {'✓ Available' if detector.langdetect_ready else '✗ Not available'}")
    print()

    # Test 1: Short messages
    print("=" * 80)
    print("TEST 1: SHORT SINGLE-WORD/PHRASE MESSAGES")
    print("=" * 80)
    print()

    short_results = []
    for message, expected, description in SHORT_MESSAGES:
        try:
            lang, conf = await detector.detect_language(message, preprocess=False)
            match = "✓" if lang == expected else "✗"
            print(f"{match} {description:30} | '{message}' -> {lang} ({conf:.0%})")
            short_results.append({"match": lang == expected, "detected": lang, "expected": expected})
        except ValueError as e:
            print(f"⚠ {description:30} | '{message}' -> SKIPPED (too short)")
            short_results.append({"match": False, "detected": "SKIPPED", "expected": expected})
        except Exception as e:
            print(f"✗ {description:30} | '{message}' -> ERROR: {e}")
            short_results.append({"match": False, "detected": "ERROR", "expected": expected})

    valid_short = [r for r in short_results if r["detected"] not in ("SKIPPED", "ERROR")]
    if valid_short:
        short_accuracy = sum(1 for r in valid_short if r["match"]) / len(valid_short) * 100
        print(f"\nShort text accuracy: {short_accuracy:.1f}% ({sum(1 for r in valid_short if r['match'])}/{len(valid_short)} valid)")
    skipped = sum(1 for r in short_results if r["detected"] == "SKIPPED")
    if skipped:
        print(f"Skipped (too short): {skipped}")
    print()

    # Test 2: Long messages
    print("=" * 80)
    print("TEST 2: LONG PARAGRAPH MESSAGES")
    print("=" * 80)
    print()

    long_results = []
    for message, expected, description in LONG_MESSAGES:
        try:
            lang, conf = await detector.detect_language(message)
            match = "✓" if lang == expected else "✗"
            cleaned = detector.preprocess_text(message)
            print(f"{match} {description}")
            print(f"   Detected: {lang} ({conf:.0%})")
            print(f"   Cleaned preview: {cleaned[:60]}...")
            print()
            long_results.append({"match": lang == expected, "conf": conf})
        except Exception as e:
            print(f"✗ {description}")
            print(f"   ERROR: {e}")
            print()
            long_results.append({"match": False, "conf": 0})

    long_accuracy = sum(1 for r in long_results if r["match"]) / len(long_results) * 100
    avg_conf = sum(r["conf"] for r in long_results) / len(long_results)
    print(f"Long text accuracy: {long_accuracy:.1f}% ({sum(1 for r in long_results if r['match'])}/{len(long_results)})")
    print(f"Average confidence: {avg_conf:.1%}")
    print()

    # Test 3: Mixed content
    print("=" * 80)
    print("TEST 3: MIXED CONTENT (EMOTES, URLS, MENTIONS)")
    print("=" * 80)
    print()

    mixed_results = []
    for message, expected, description in MIXED_MESSAGES:
        try:
            lang, conf = await detector.detect_language(message)
            match = "✓" if lang == expected else "✗"
            cleaned = detector.preprocess_text(message)
            print(f"{match} {description}")
            print(f"   Original: {message[:60]}...")
            print(f"   Cleaned:  {cleaned[:60]}...")
            print(f"   Detected: {lang} ({conf:.0%})")
            print()
            mixed_results.append({"match": lang == expected, "conf": conf})
        except ValueError as e:
            print(f"⚠ {description}")
            print(f"   SKIPPED: {e}")
            print()
            mixed_results.append({"match": False, "conf": 0})
        except Exception as e:
            print(f"✗ {description}")
            print(f"   ERROR: {e}")
            print()
            mixed_results.append({"match": False, "conf": 0})

    valid_mixed = [r for r in mixed_results if r["conf"] > 0]
    if valid_mixed:
        mixed_accuracy = sum(1 for r in valid_mixed if r["match"]) / len(valid_mixed) * 100
        print(f"Mixed content accuracy: {mixed_accuracy:.1f}% ({sum(1 for r in valid_mixed if r['match'])}/{len(valid_mixed)})")
    print()

    # Final summary
    print("=" * 80)
    print("FINAL SUMMARY")
    print("=" * 80)
    all_valid = valid_short + long_results + valid_mixed
    total_correct = sum(1 for r in all_valid if r["match"])
    total_tests = len(all_valid)
    print(f"Overall accuracy: {total_correct}/{total_tests} ({total_correct/total_tests*100:.1f}%)")
    print()


if __name__ == "__main__":
    asyncio.run(run_tests())
