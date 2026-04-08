import torch
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from train import SafeEdgeVLA, DummyDataset
from torch.utils.data import DataLoader


class TestSafeEdgeVLA(unittest.TestCase):
    def setUp(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = SafeEdgeVLA().to(self.device)

    def test_model_forward_pass(self):
        batch_size = 2
        images = torch.randn(batch_size, 3, 256, 256).to(self.device)
        tokens = torch.randint(0, 50257, (batch_size, 64)).to(self.device)
        action_logits, safety_score = self.model(images, tokens)
        self.assertEqual(action_logits.shape, (batch_size, 1, 7, 256))
        self.assertEqual(safety_score.shape, (batch_size, 1))
        self.assertTrue((safety_score >= 0.0).all() and (safety_score <= 1.0).all())

    def test_dataset(self):
        dataset = DummyDataset(size=10)
        self.assertEqual(len(dataset), 10)
        sample = dataset[0]
        self.assertEqual(sample["image"].shape, (3, 256, 256))
        self.assertEqual(sample["tokens"].shape, (64,))
        self.assertEqual(sample["actions"].shape, (7,))
        self.assertIsInstance(sample["is_safe"], torch.Tensor)

    def test_dataloader(self):
        dataset = DummyDataset(size=20)
        dataloader = DataLoader(dataset, batch_size=4, shuffle=False)
        batch = next(iter(dataloader))
        self.assertEqual(batch["image"].shape, (4, 3, 256, 256))
        self.assertEqual(batch["tokens"].shape, (4, 64))
        self.assertEqual(batch["actions"].shape, (4, 7))
        self.assertEqual(batch["is_safe"].shape, (4,))

    def test_training_step(self):
        batch_size = 2
        images = torch.randn(batch_size, 3, 256, 256).to(self.device)
        tokens = torch.randint(0, 50257, (batch_size, 64)).to(self.device)
        actions = torch.randint(0, 256, (batch_size, 7)).to(self.device)
        is_safe = torch.tensor([1.0, 0.0]).to(self.device)

        optimizer = torch.optim.AdamW(self.model.parameters(), lr=3e-4)
        action_criterion = torch.nn.CrossEntropyLoss()
        safety_criterion = torch.nn.BCELoss()

        self.model.train()
        optimizer.zero_grad()
        action_logits, safety_score = self.model(images, tokens)
        # action_logits shape: (batch, 1, 7, 256) → (batch, 256, 7) for CrossEntropyLoss
        action_logits = action_logits.squeeze(1).permute(0, 2, 1)
        action_loss = action_criterion(action_logits, actions)
        safety_loss = safety_criterion(safety_score.squeeze(), is_safe)
        loss = action_loss + safety_loss
        loss.backward()
        optimizer.step()

        self.assertFalse(torch.isnan(loss))
        self.assertFalse(torch.isinf(loss))


if __name__ == "__main__":
    unittest.main()
