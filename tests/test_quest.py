import unittest

from collectors.quest import extract_menu_days


class QuestExtractionTests(unittest.TestCase):
    def test_nested_menu_payload(self):
        payload = {
            "data": {
                "days": [
                    {
                        "serviceDate": "2026-09-02T00:00:00",
                        "mealPeriodName": "Lunch",
                        "menuItems": [
                            {"recipeName": "Bosco Sticks with Marinara"},
                            {"recipeName": "Beef Hot Dog on Bun"},
                        ],
                    }
                ]
            }
        }
        meals = extract_menu_days([payload])
        self.assertEqual(len(meals), 1)
        self.assertEqual(meals[0]["date"], "2026-09-02")
        self.assertEqual(meals[0]["group"], "k5")
        self.assertEqual(
            meals[0]["items"],
            ["Bosco Sticks with Marinara", "Beef Hot Dog on Bun"],
        )

    def test_ignores_breakfast(self):
        payload = {
            "days": [
                {
                    "date": "2026-09-02",
                    "mealType": "Breakfast",
                    "items": [{"itemName": "Breakfast Pizza"}],
                }
            ]
        }
        self.assertEqual(extract_menu_days([payload]), [])


if __name__ == "__main__":
    unittest.main()
