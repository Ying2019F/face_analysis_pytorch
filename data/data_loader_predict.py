from torchvision import transforms
import numpy as np
from PIL import Image
from torch.utils.data import DataLoader
from torchvision.datasets import ImageFolder
import pandas as pd
from os import path


class ImageList(ImageFolder):
    def __init__(self, source, image_list):
        image_names = pd.read_csv(image_list, delimiter=' ', header=None)
        image_names = np.array(image_names)

        self.samples = [path.join(source, image_name) for image_name in image_names[:, 0]]

        self.transform = transforms.Compose([
            transforms.Resize((112, 112)),
            transforms.ToTensor(),
            transforms.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
        ])

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        img = Image.open(self.samples[index]).convert('RGB')

        return self.transform(img)


class PredictionDataLoader(DataLoader):
    def __init__(self, batch_size, workers, source, image_list):
        self._dataset = ImageList(source, image_list)

        super(PredictionDataLoader, self).__init__(self._dataset, batch_size=batch_size,
                                                   shuffle=False, pin_memory=True,
                                                   num_workers=workers, drop_last=False)
