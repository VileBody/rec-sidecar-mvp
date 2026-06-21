package clean

import (
	"errors"
	"strings"
)

const (
	StudentDirectionEnRu = "en-ru"
	StudentDirectionRuEn = "ru-en"
)

func normalizeStudentDirection(direction string) (string, error) {
	direction = strings.ToLower(strings.TrimSpace(direction))
	switch direction {
	case "", "en-ru", "en_ru", "en->ru", "english-russian":
		return StudentDirectionEnRu, nil
	case "ru-en", "ru_en", "ru->en", "russian-english":
		return StudentDirectionRuEn, nil
	default:
		return "", errors.New("direction must be en-ru or ru-en")
	}
}

func sourceLanguageForDirection(direction string) string {
	direction, _ = normalizeStudentDirection(direction)
	if direction == StudentDirectionRuEn {
		return "ru"
	}
	return "en"
}

func targetLanguageForDirection(direction string) string {
	direction, _ = normalizeStudentDirection(direction)
	if direction == StudentDirectionRuEn {
		return "en"
	}
	return "ru"
}
