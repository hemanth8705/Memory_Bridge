import 'package:flutter_test/flutter_test.dart';
import 'package:memorybridge/services/recording_scanner.dart';

void main() {
  group('phone extraction (must agree with backend app/contacts.py)', () {
    test('pulls the number out of common recorder filenames', () {
      expect(
        RecordingScanner.extractPhoneNumber('Call_+919876543210_20260801_103000.mp3'),
        '+919876543210',
      );
      expect(
        RecordingScanner.extractPhoneNumber('919876543210_20260801.mp3'),
        '919876543210',
      );
      expect(
        RecordingScanner.extractPhoneNumber('Call recording +91 98765 43210.m4a'),
        '+919876543210',
      );
      expect(
        RecordingScanner.extractPhoneNumber('+91-98765-43210_out.amr'),
        '+919876543210',
      );
    });

    test('does not mistake a datestamp for a phone number', () {
      expect(RecordingScanner.extractPhoneNumber('20260801_103000.mp3'), isNull);
      expect(RecordingScanner.extractPhoneNumber('Rahul_call_2026_08_01.mp3'), isNull);
      expect(RecordingScanner.extractPhoneNumber('voice_memo.m4a'), isNull);
    });
  });

  group('number normalisation', () {
    test('collapses every local format to one key', () {
      const variants = [
        '+919876543210',
        '919876543210',
        '09876543210',
        '9876543210',
        '+91 98765 43210',
      ];
      final keys = variants.map(RecordingScanner.normalizePhone).toSet();
      expect(keys.length, 1);
      expect(keys.first, '9876543210');
    });

    test('rejects things that are too short to be a number', () {
      expect(RecordingScanner.normalizePhone('123'), isNull);
      expect(RecordingScanner.normalizePhone(null), isNull);
    });
  });

  group('name fallback', () {
    test('recovers a name from a name-based filename', () {
      expect(RecordingScanner.guessName('Rahul_call_2026_08_01.mp3'), 'Rahul');
      expect(RecordingScanner.guessName('Call recording Sarah.m4a'), 'Sarah');
    });

    test('returns null when there is nothing name-like left', () {
      expect(RecordingScanner.guessName('call_recording_001.mp3'), isNull);
    });
  });

  group('recording hash', () {
    final when = DateTime.fromMillisecondsSinceEpoch(1754037000000);

    test('is deterministic for the same file', () {
      final a = RecordingScanner.hashOf('call.mp3', 12345, when);
      final b = RecordingScanner.hashOf('call.mp3', 12345, when);
      expect(a, b);
      expect(a.length, 64);
    });

    test('changes when any input changes', () {
      final base = RecordingScanner.hashOf('call.mp3', 12345, when);
      expect(RecordingScanner.hashOf('other.mp3', 12345, when), isNot(base));
      expect(RecordingScanner.hashOf('call.mp3', 999, when), isNot(base));
      expect(
        RecordingScanner.hashOf('call.mp3', 12345, when.add(const Duration(seconds: 1))),
        isNot(base),
      );
    });
  });

  group('SAF tree URI resolution', () {
    test('maps primary storage to a real path', () {
      expect(
        RecordingScanner.resolveFolderPath(
          'content://com.android.externalstorage.documents/tree/primary%3ARecordings%2FCall',
        ),
        '/storage/emulated/0/Recordings/Call',
      );
    });

    test('maps an SD card volume', () {
      expect(
        RecordingScanner.resolveFolderPath(
          'content://com.android.externalstorage.documents/tree/1234-5678%3ACallRec',
        ),
        '/storage/1234-5678/CallRec',
      );
    });

    test('strips a trailing document segment', () {
      expect(
        RecordingScanner.resolveFolderPath(
          'content://com.android.externalstorage.documents/tree/primary%3ARecordings'
          '/document/primary%3ARecordings',
        ),
        '/storage/emulated/0/Recordings',
      );
    });

    test('passes a plain path straight through', () {
      expect(
        RecordingScanner.resolveFolderPath('/storage/emulated/0/CallRecordings'),
        '/storage/emulated/0/CallRecordings',
      );
    });

    test('returns null for a URI it cannot map', () {
      expect(RecordingScanner.resolveFolderPath('content://some/other/provider'), isNull);
    });
  });
}
